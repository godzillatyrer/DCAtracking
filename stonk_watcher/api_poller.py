"""Track 1 — backend API sniper.

Polls the Stonk Launcher backend for tokens/highlights, alerts on anything
never seen before, auto-tracks new CAs, and screams *** CLOCKIN *** on a
name/symbol match. Independent from the block-scanning loop so slow RPC
never delays API polls.
"""
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from . import config
from .alerts import AlertPipeline, ErrorReporter, format_clockin_alert, is_target_token
from .state import State

log = logging.getLogger("api_poller")

ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")
CA_KEYS = ("address", "ca", "contract", "contract_address", "contractAddress",
           "token_address", "tokenAddress", "id")
NAME_KEYS = ("name", "token_name", "tokenName", "title")
SYMBOL_KEYS = ("symbol", "ticker", "token_symbol", "tokenSymbol")
SUPPLY_KEYS = ("supply", "total_supply", "totalSupply", "max_supply", "maxSupply")


def _first_key(obj: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[Any]:
    for key in keys:
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
    return None


def extract_ca(obj: Dict[str, Any]) -> Optional[str]:
    value = _first_key(obj, CA_KEYS)
    if isinstance(value, str) and ADDRESS_RE.fullmatch(value.strip()):
        return value.strip().lower()
    # Schema is unconfirmed — fall back to any address-shaped string anywhere.
    match = ADDRESS_RE.search(json.dumps(obj))
    return match.group(0).lower() if match else None


class ApiPoller:
    name = "api_poller"

    def __init__(self, state: State, pipeline: AlertPipeline,
                 errors: ErrorReporter) -> None:
        self.state = state
        self.pipeline = pipeline
        self.errors = errors
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config.BROWSER_USER_AGENT,
            "Accept": "application/json",
        })
        self.consecutive_throttles = 0
        self.consecutive_errors = 0
        self.throttle_alerted = False
        self.down_alerted = False

    # -- fetching ------------------------------------------------------------
    def _fetch_json(self, url: str) -> Optional[Any]:
        """Soft-failure fetch: returns parsed JSON or None. Raises nothing."""
        try:
            resp = self.session.get(url, timeout=15)
        except requests.RequestException as exc:
            log.warning("fetch failed %s: %s", url, exc)
            self.consecutive_errors += 1
            return None
        if resp.status_code in (403, 429):
            self.consecutive_throttles += 1
            log.warning("throttled (%d in a row) on %s: HTTP %d",
                        self.consecutive_throttles, url, resp.status_code)
            return None
        if resp.status_code != 200:
            log.warning("non-200 on %s: HTTP %d", url, resp.status_code)
            self.consecutive_errors += 1
            return None
        try:
            data = resp.json()
        except ValueError:
            log.warning("non-JSON body on %s (HTML error page?)", url)
            self.consecutive_errors += 1
            return None
        self.consecutive_throttles = 0
        self.consecutive_errors = 0
        if self.throttle_alerted:
            self.throttle_alerted = False
            self.errors.recovered(self.name, "backend API responding normally again")
        if self.down_alerted:
            self.down_alerted = False
            self.errors.recovered(self.name, "backend API reachable again")
        return data

    def fetch_detail(self, ca: str) -> Optional[Dict[str, Any]]:
        data = self._fetch_json(config.API_TOKEN_DETAIL_URL.format(ca=ca))
        if isinstance(data, dict) and data.get("ok") is not False:
            return data
        return data if isinstance(data, dict) else None

    # -- token handling ------------------------------------------------------
    def _collect_entries(self, tokens_body: Any, highlights_body: Any) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        if isinstance(tokens_body, dict):
            tokens = tokens_body.get("tokens")
            if isinstance(tokens, list):
                entries.extend(t for t in tokens if isinstance(t, dict))
        if isinstance(highlights_body, dict):
            for slot in ("king", "top", "hot"):
                value = highlights_body.get(slot)
                if isinstance(value, dict):
                    value = dict(value)
                    value.setdefault("_highlight_slot", slot)
                    entries.append(value)
        return entries

    def _handle_new_token(self, entry: Dict[str, Any], ca: str) -> None:
        detail = self.fetch_detail(ca)
        merged: Dict[str, Any] = {}
        if isinstance(detail, dict):
            merged.update(detail.get("token") if isinstance(detail.get("token"), dict)
                          else detail)
        merged.update(entry)

        name = str(_first_key(merged, NAME_KEYS) or "?")
        symbol = str(_first_key(merged, SYMBOL_KEYS) or "?")
        supply = str(_first_key(merged, SUPPLY_KEYS) or "?")
        clockin = is_target_token(name, symbol)

        # Arm LP detection instantly.
        self.state.add_tracked(ca, source="api", name=name, symbol=symbol)
        self.state.mark_api_token_seen(ca)
        self.state.save_if_dirty()

        raw = {"entry": entry, "detail": detail}
        if clockin:
            text = format_clockin_alert(ca, "api", name, symbol, supply, raw=raw)
            self.pipeline.send(text, level="loud", code_lines=[ca],
                               category="launch")
        else:
            text = (f"NEW LAUNCHER TOKEN (API)\n{ca}\n"
                    f"{name} ({symbol}) | supply: {supply}\n"
                    f"token: {config.BLOCKSCOUT_TOKEN_URL.format(ca=ca)}\n"
                    f"site:  {config.LAUNCHER_PAGE_URL}\n"
                    f"raw:   {json.dumps(raw, separators=(',', ':'))[:1500]}")
            self.pipeline.send(text, level="loud", code_lines=[ca],
                               category="launch")

    def _handle_ca_less_entry(self, entry: Dict[str, Any]) -> None:
        digest = str(hash(json.dumps(entry, sort_keys=True)))
        seen = self.state.data["api_seen_raw_hashes"]
        if digest in seen:
            return
        seen.append(digest)
        self.state.mark_dirty()
        self.state.save_if_dirty()
        self.pipeline.send(
            "NEW LAUNCHER ENTRY (API, no CA found in payload)\n"
            f"raw: {json.dumps(entry, separators=(',', ':'))[:2000]}",
            level="alert")

    # -- main cycle ----------------------------------------------------------
    def run_once(self) -> None:
        tokens_body = self._fetch_json(config.API_TOKENS_URL)
        highlights_body = self._fetch_json(config.API_HIGHLIGHTS_URL)

        if (self.consecutive_throttles > config.FAILURE_ALERT_THRESHOLD
                and not self.throttle_alerted):
            self.throttle_alerted = True
            self.errors.report(
                self.name,
                f"API POLLER THROTTLED — {self.consecutive_throttles} consecutive "
                f"403/429 responses from {config.API_BASE}; backing off to 30s. "
                "The API path is degraded; the on-chain path is still live.",
                key="api_throttled")
        if (self.consecutive_errors >= config.FAILURE_ALERT_THRESHOLD
                and not self.down_alerted):
            self.down_alerted = True
            self.errors.report(
                self.name,
                f"Backend API not responding ({self.consecutive_errors} consecutive "
                f"failures on {config.API_BASE}) — no data from the API path.",
                key="api_down")

        if tokens_body is None and highlights_body is None:
            return

        # Compare actual CA sets, never array lengths — entries may be
        # pre-staged and then removed.
        entries = self._collect_entries(tokens_body, highlights_body)
        seen_this_cycle: Set[str] = set()
        for entry in entries:
            ca = extract_ca(entry)
            if ca is None:
                self._handle_ca_less_entry(entry)
                continue
            if ca in seen_this_cycle:
                continue
            seen_this_cycle.add(ca)
            if self.state.api_token_seen(ca):
                continue
            self._handle_new_token(entry, ca)

    def interval(self, now: Optional[datetime] = None) -> float:
        now = now or datetime.now(timezone.utc)
        base = config.api_poll_interval(now, config.get_t0())
        if self.throttle_alerted:
            base = max(base, 30.0)
        return base
