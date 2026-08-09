"""Optional HTTP health endpoint.

Only needed when the watcher runs somewhere that requires an open port
(e.g. a Render *web service*, which fails to deploy if nothing binds to
$PORT). A background worker does not need this.

Returns 200 while every component is completing cycles on schedule, and 503
once the watchdog considers one stalled — so an external uptime monitor
doubles as a second, independent "it stopped working" alarm.

Deliberately exposes NO contract addresses: the response is counts and
health only, because a Render web service URL is public and the CA is the
whole edge.
"""
import json
import logging
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict

from . import config

log = logging.getLogger("health")


def build_report(supervisor) -> Dict[str, Any]:
    now = time.monotonic()
    t0 = config.get_t0()
    seconds_to_t0 = (t0 - datetime.now(timezone.utc)).total_seconds()
    components = {}
    healthy = True
    for comp in supervisor.components:
        age = now - comp.last_completed
        allowed = max(180.0, comp.interval() * 6)
        ok = age <= allowed
        healthy = healthy and ok
        components[comp.name] = {
            "last_cycle_seconds_ago": round(age, 1),
            "expected_every_seconds": round(comp.interval(), 1),
            "ok": ok,
        }
    state = supervisor.state.data
    return {
        "healthy": healthy,
        "t0": t0.isoformat(),
        "hours_to_t0": round(seconds_to_t0 / 3600, 2),
        "war_room": -2 * 3600 < seconds_to_t0 <= 3600,
        "components": components,
        "telegram_configured": bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID),
        # Counts only — never the addresses themselves.
        "tracked_count": len(state["tracked"]),
        "api_tokens_seen_count": len(state["api_seen_tokens"]),
        "candidate_factories_count": len(state["candidate_factories"]),
        "target": config.TARGET_TOKEN_NAME,
    }


def make_handler(supervisor):
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib naming)
            report = build_report(supervisor)
            status = 200 if report["healthy"] else 503
            body = json.dumps(report, indent=1).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            # Keep uptime-monitor pings out of the watcher's log stream.
            log.debug(fmt, *args)

    return HealthHandler


def start_health_server(supervisor, port: int) -> HTTPServer:
    server = HTTPServer(("0.0.0.0", port), make_handler(supervisor))
    thread = threading.Thread(target=server.serve_forever, name="health",
                              daemon=True)
    thread.start()
    log.info("health endpoint listening on 0.0.0.0:%d", port)
    return server
