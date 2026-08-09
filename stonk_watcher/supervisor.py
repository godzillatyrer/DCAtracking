"""Thread supervisor, watchdog and heartbeat.

Each component (API poller, chain watcher, frontend differ) runs on its own
thread so slow RPC never delays API polls. The supervisor:

- catches every component exception, reports it to Telegram (rate-limited)
  and keeps the loop alive;
- runs a watchdog that alerts if any component stops completing cycles
  (stall detection — "error message if it's not working");
- prints a console heartbeat every cycle and optionally sends a periodic
  "WATCHER ALIVE" Telegram message so silence itself is detectable;
- saves state and announces start/stop/crash on Telegram.
"""
import logging
import random
import signal
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from . import config
from .alerts import AlertPipeline, ErrorReporter
from .state import State, as_checklist

log = logging.getLogger("supervisor")

WATCHDOG_CHECK_EVERY = 30.0


class Component:
    def __init__(self, name: str, run_once: Callable[[], None],
                 interval: Callable[[], float]) -> None:
        self.name = name
        self.run_once = run_once
        self.interval = interval
        self.last_completed = time.monotonic()
        self.stalled_alerted = False


class Supervisor:
    def __init__(self, state: State, pipeline: AlertPipeline,
                 errors: ErrorReporter) -> None:
        self.state = state
        self.pipeline = pipeline
        self.errors = errors
        self.components: List[Component] = []
        self.stop_event = threading.Event()
        self._threads: List[threading.Thread] = []
        self._last_heartbeat_sent = time.monotonic()

    def add(self, component_obj) -> None:
        self.components.append(Component(
            component_obj.name, component_obj.run_once, component_obj.interval))

    # -- worker loop ---------------------------------------------------------
    def _worker(self, comp: Component) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                comp.run_once()
                comp.last_completed = time.monotonic()
                if comp.stalled_alerted:
                    comp.stalled_alerted = False
                    self.errors.recovered(comp.name, "cycles completing again")
            except Exception as exc:  # never let a component kill its thread
                log.error("component %s crashed a cycle:\n%s",
                          comp.name, traceback.format_exc())
                self.errors.report_exception(comp.name, exc)
            # Jitter ±20% so the cadence doesn't look mechanical.
            interval = comp.interval() * random.uniform(0.8, 1.2)
            elapsed = time.monotonic() - started
            self.stop_event.wait(max(0.2, interval - elapsed))

    # -- watchdog + heartbeat ------------------------------------------------
    def _watchdog(self) -> None:
        while not self.stop_event.is_set():
            self.stop_event.wait(WATCHDOG_CHECK_EVERY)
            if self.stop_event.is_set():
                return
            now = time.monotonic()
            for comp in self.components:
                allowed = max(180.0, comp.interval() * 6)
                stalled_for = now - comp.last_completed
                if stalled_for > allowed and not comp.stalled_alerted:
                    comp.stalled_alerted = True
                    self.errors.report(
                        "watchdog",
                        f"COMPONENT STALLED: {comp.name} has not completed a "
                        f"cycle in {int(stalled_for)}s (expected roughly every "
                        f"{int(comp.interval())}s). The watcher may be "
                        "partially blind — check the process/network.",
                        key=f"stall:{comp.name}")
            self._maybe_send_heartbeat()

    def _maybe_send_heartbeat(self) -> None:
        if config.HEARTBEAT_HOURS <= 0:
            return
        if time.monotonic() - self._last_heartbeat_sent < config.HEARTBEAT_HOURS * 3600:
            return
        self._last_heartbeat_sent = time.monotonic()
        t0 = config.get_t0()
        dt = (t0 - datetime.now(timezone.utc)).total_seconds()
        when = (f"T0 in {dt / 3600:.1f}h" if dt > 0 else f"T0 was {-dt / 3600:.1f}h ago")
        lines = [f"WATCHER ALIVE — {when}"] + as_checklist(self.state)
        self.pipeline.send("\n".join(lines), level="info", category="health")

    def _console_heartbeat(self) -> None:
        while not self.stop_event.is_set():
            t0 = config.get_t0()
            dt = (t0 - datetime.now(timezone.utc)).total_seconds()
            ages = ", ".join(
                f"{c.name}={int(time.monotonic() - c.last_completed)}s ago"
                for c in self.components)
            log.info("HEARTBEAT | T0 %+dm | last cycles: %s | %s",
                     int(dt / 60), ages, "; ".join(as_checklist(self.state)))
            self.stop_event.wait(60)

    # -- lifecycle -----------------------------------------------------------
    def run(self) -> None:
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        t0 = config.get_t0()
        self.pipeline.send(
            "WATCHER STARTED\n"
            f"T0 = {t0.isoformat()} (CLOCKIN launch window)\n"
            f"components: {', '.join(c.name for c in self.components)}\n"
            + "\n".join(as_checklist(self.state)),
            level="info", category="health")

        report = self.state.take_migration_report()
        if report and (report.get("purged_factories") or report.get("purged_tokens")):
            factories = report.get("purged_factories") or []
            self.pipeline.send(
                "STATE CLEANED ON UPGRADE\n"
                f"removed {len(factories)} factory watch(es) and "
                f"{len(report.get('purged_tokens') or [])} tracked token(s) "
                "that had no launchpad provenance.\n"
                + ("".join(f"\n  {a}" for a in factories[:10])
                   + "\nIf one of these was the real launcher factory, set "
                     "WATCH_CONTRACTS to re-arm it."
                   if factories else ""),
                level="info", category="health")
            self.state.save_if_dirty()

        for comp in self.components:
            thread = threading.Thread(target=self._worker, args=(comp,),
                                      name=comp.name, daemon=True)
            thread.start()
            self._threads.append(thread)
        for target, name in ((self._watchdog, "watchdog"),
                             (self._console_heartbeat, "heartbeat")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

        try:
            while not self.stop_event.is_set():
                time.sleep(1)
        except Exception as exc:
            self.pipeline.send(
                f"WATCHER CRASHED\n{type(exc).__name__}: {exc}\n"
                "The watcher process is DOWN — restart it.", level="loud",
                category="health")
            raise
        finally:
            self.stop_event.set()
            for thread in self._threads:
                thread.join(timeout=5)
            self.state.save()
            self.pipeline.send(
                "WATCHER STOPPED\n"
                "No further alerts will fire until it is restarted.",
                level="alert", category="health")

    def _handle_signal(self, signum, _frame) -> None:
        log.info("received signal %s — shutting down", signum)
        self.stop_event.set()
