#!/usr/bin/env python3
"""Stonk Launcher / CLOCKIN launch sniper — entrypoint.

Usage:
  python3 watcher.py               run the watcher (24/7)
  python3 watcher.py --test-alert  send a test alert on every channel
  python3 watcher.py --status      print state summary and poll cadence
  python3 watcher.py --once        run one cycle of each component and exit
"""
import argparse
import logging
import sys
from datetime import datetime, timezone

from stonk_watcher import config
from stonk_watcher.alerts import AlertPipeline, ErrorReporter
from stonk_watcher.api_poller import ApiPoller
from stonk_watcher.chain_watcher import ChainWatcher
from stonk_watcher.frontend_diff import FrontendDiffer
from stonk_watcher.state import State, as_checklist
from stonk_watcher.supervisor import Supervisor


def build_components(state, pipeline, errors):
    return [
        ApiPoller(state, pipeline, errors),
        ChainWatcher(state, pipeline, errors),
        FrontendDiffer(state, pipeline, errors),
    ]


def cmd_test_alert(pipeline: AlertPipeline) -> int:
    results = pipeline.send_test()
    ok = True
    for channel, result in results.items():
        print(f"  {channel:10s} {result}")
        if channel == "telegram" and result != "ok":
            ok = False
    if not ok:
        print("\nFAILED: Telegram is the primary channel and it did not deliver.")
        return 1
    print("\nAll configured channels delivered.")
    return 0


def cmd_status(state: State) -> int:
    now = datetime.now(timezone.utc)
    t0 = config.get_t0()
    dt = (t0 - now).total_seconds()
    print(f"now: {now.isoformat()}")
    print(f"T0:  {t0.isoformat()}  ({dt / 3600:+.1f}h)")
    print(f"api poll interval:   {config.api_poll_interval(now, t0):.0f}s")
    print(f"chain poll interval: {config.chain_poll_interval(now, t0):.0f}s")
    print(f"telegram configured: {bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)}")
    print(f"rpc url: {config.RPC_URL}")
    for line in as_checklist(state):
        print(f"  {line}")
    tracked = state.data["tracked"]
    if tracked:
        print("tracked CAs:")
        for ca, info in tracked.items():
            print(f"  {ca}  [{info.get('source')}] {info.get('name', '?')} "
                  f"({info.get('symbol', '?')})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-alert", action="store_true",
                        help="send a test alert to all channels and exit")
    parser.add_argument("--status", action="store_true",
                        help="print state summary and exit")
    parser.add_argument("--once", action="store_true",
                        help="run one cycle of each component and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S")

    pipeline = AlertPipeline()
    if args.test_alert:
        return cmd_test_alert(pipeline)

    state = State()
    if args.status:
        return cmd_status(state)

    errors = ErrorReporter(pipeline)
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        print("WARNING: Telegram is not configured (TELEGRAM_BOT_TOKEN / "
              "TELEGRAM_CHAT_ID). Alerts will only be logged to the console.",
              file=sys.stderr)

    components = build_components(state, pipeline, errors)
    if args.once:
        for component in components:
            print(f"-- running {component.name} once")
            component.run_once()
        state.save()
        return 0

    supervisor = Supervisor(state, pipeline, errors)
    for component in components:
        supervisor.add(component)
    supervisor.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
