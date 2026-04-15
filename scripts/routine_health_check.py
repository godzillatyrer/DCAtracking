"""
Routine Health Check — CLI wrapper for /api/diagnostics/health-audit.

Designed to be invoked by automated agents (Claude Code routines,
monitoring tools, cron). Prints a concise human-readable summary to
stderr AND a structured JSON blob to stdout, so an agent can either
`| jq` the JSON or read the summary.

Exit codes:
  0  all healthy
  1  at least one critical issue
  2  warnings only (no critical)
  3  could not reach the service

Usage:
  DCATRACKING_URL=https://your-app.onrender.com python scripts/routine_health_check.py
  python scripts/routine_health_check.py --auto-heal       # POST safe-fix URLs
  python scripts/routine_health_check.py --json > out.json # machine-readable

If DCATRACKING_URL is not set, defaults to http://localhost:8000 (local dev).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx


def _base_url() -> str:
    return os.environ.get("DCATRACKING_URL", "http://localhost:8000").rstrip("/")


def _fetch_audit() -> dict[str, Any]:
    url = f"{_base_url()}/api/diagnostics/health-audit"
    resp = httpx.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _apply_fix(fix_url: str) -> dict[str, Any]:
    full = f"{_base_url()}{fix_url}"
    resp = httpx.post(full, timeout=60)
    resp.raise_for_status()
    return resp.json()


def _print_summary(audit: dict[str, Any]) -> None:
    """Human-readable summary to stderr."""
    s = audit.get("summary", {})
    healthy = audit.get("healthy")
    print("\n=== DCAtracking Health Audit ===", file=sys.stderr)
    print(f"Checked: {audit.get('checked_at')}", file=sys.stderr)
    print(
        f"Status: {'HEALTHY' if healthy else 'UNHEALTHY'}  "
        f"Total: {s.get('total', 0)}  "
        f"Critical: {s.get('critical', 0)}  "
        f"Warning: {s.get('warning', 0)}  "
        f"Info: {s.get('info', 0)}",
        file=sys.stderr,
    )
    for issue in audit.get("issues", []):
        sev = issue["severity"].upper()
        icon = {"critical": "\u274c", "warning": "\u26a0\ufe0f", "info": "\u2139\ufe0f"}.get(
            issue["severity"], "?"
        )
        print(
            f"\n{icon} [{sev}] {issue['title']}",
            file=sys.stderr,
        )
        print(f"   id: {issue['id']}", file=sys.stderr)
        print(f"   fix: {issue['likely_fix']}", file=sys.stderr)
        if issue.get("auto_fix_url"):
            print(f"   auto_fix_url: {issue['auto_fix_url']}", file=sys.stderr)
        if issue.get("files_to_check"):
            print(
                f"   files_to_check: {', '.join(issue['files_to_check'])}",
                file=sys.stderr,
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="emit raw JSON to stdout, nothing to stderr")
    parser.add_argument(
        "--auto-heal",
        action="store_true",
        help="for each issue with an auto_fix_url, POST it and re-audit once",
    )
    args = parser.parse_args()

    try:
        audit = _fetch_audit()
    except Exception as e:
        print(f"Could not reach health-audit endpoint: {e}", file=sys.stderr)
        return 3

    if args.auto_heal:
        fixed = []
        for issue in audit.get("issues", []):
            url = issue.get("auto_fix_url")
            if not url:
                continue
            try:
                result = _apply_fix(url)
                fixed.append({"id": issue["id"], "fix_result": result})
            except Exception as e:
                fixed.append({"id": issue["id"], "fix_error": str(e)})
        if fixed:
            # Re-run audit after fixes
            try:
                audit = _fetch_audit()
                audit["auto_heal_results"] = fixed
            except Exception:
                pass

    if args.json:
        json.dump(audit, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        _print_summary(audit)

    summary = audit.get("summary", {})
    if summary.get("critical", 0) > 0:
        return 1
    if summary.get("warning", 0) > 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
