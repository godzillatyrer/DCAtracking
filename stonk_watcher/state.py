"""Persistent JSON state with atomic writes.

Everything the watcher must remember across restarts lives here so that a
mid-run restart never re-fires alerts (acceptance test 5).
"""
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

from . import config

_LOCK = threading.RLock()


def _default_state() -> Dict[str, Any]:
    return {
        # Track 1
        "api_seen_tokens": [],            # list[str] lowercase CAs seen via API
        "api_seen_raw_hashes": [],        # dedupe for CA-less raw entries
        # CA registry shared with the base watcher
        "tracked": {},                    # ca -> {source, name, symbol, added_at, first_activity_alerted}
        # Track 2
        "dev_wallets": {},                # wallet -> {baselined, seen_tx_hashes}
        "candidate_factories": {},        # addr -> {deploy_block, last_scanned, learned_topic0, confirmed_tokens, seen_log_keys}
        "amm_last_scanned": None,         # block cursor for optional AMM factory watch
        # Chain-wide mint watch (catches tokens from unknown factories)
        "mint_last_scanned": None,        # block cursor; None = start at head
        "mint_seen_tokens": [],           # contracts already seen minting
        # Track 3
        "frontend": {
            "deployment_id": None,
            "baselined": False,           # first run records silently
            "seen_addresses": [],
            "negative_cache": [],         # addresses that 404 on Blockscout
        },
        "meta": {"created_at": time.time()},
    }


class State:
    def __init__(self, path: Optional[str] = None):
        self.path = path or config.STATE_FILE
        self.data = _default_state()
        self._dirty = False
        self.load()

    # -- persistence ---------------------------------------------------------
    def load(self) -> None:
        with _LOCK:
            if not os.path.exists(self.path):
                return
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
            except (OSError, ValueError):
                # Corrupt state: keep a copy for forensics, start fresh.
                try:
                    os.rename(self.path, self.path + ".corrupt")
                except OSError:
                    pass
                return
            base = _default_state()
            base.update(loaded)
            for key in ("frontend",):
                merged = _default_state()[key]
                merged.update(loaded.get(key) or {})
                base[key] = merged
            self.data = base

    def save(self) -> None:
        with _LOCK:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=1)
            os.replace(tmp, self.path)
            self._dirty = False

    def mark_dirty(self) -> None:
        self._dirty = True

    def save_if_dirty(self) -> None:
        with _LOCK:
            if self._dirty:
                self.save()

    # -- Track 1: API dedupe -------------------------------------------------
    def api_token_seen(self, ca: str) -> bool:
        return ca.lower() in self.data["api_seen_tokens"]

    def mark_api_token_seen(self, ca: str) -> None:
        with _LOCK:
            ca = ca.lower()
            if ca not in self.data["api_seen_tokens"]:
                self.data["api_seen_tokens"].append(ca)
                self.mark_dirty()

    # -- tracked registry ----------------------------------------------------
    def is_tracked(self, ca: str) -> bool:
        return ca.lower() in self.data["tracked"]

    def add_tracked(self, ca: str, source: str, **info: Any) -> bool:
        """Add a CA to the tracked registry. Returns True if newly added."""
        with _LOCK:
            ca = ca.lower()
            if ca in self.data["tracked"]:
                return False
            entry = {"source": source, "added_at": time.time()}
            entry.update(info)
            self.data["tracked"][ca] = entry
            self.mark_dirty()
            return True

    # -- dev wallets ---------------------------------------------------------
    def wallet_state(self, wallet: str) -> Dict[str, Any]:
        with _LOCK:
            key = wallet.lower()
            if key not in self.data["dev_wallets"]:
                self.data["dev_wallets"][key] = {"baselined": False, "seen_tx_hashes": []}
                self.mark_dirty()
            return self.data["dev_wallets"][key]

    # -- candidate factories -------------------------------------------------
    def add_candidate_factory(self, addr: str, deploy_block: int) -> bool:
        with _LOCK:
            key = addr.lower()
            if key in self.data["candidate_factories"]:
                return False
            self.data["candidate_factories"][key] = {
                "deploy_block": deploy_block,
                "last_scanned": deploy_block - 1,
                "learned_topic0": None,
                "confirmed_tokens": [],
                "seen_log_keys": [],
            }
            self.mark_dirty()
            return True

    def candidate_factories(self) -> Dict[str, Dict[str, Any]]:
        return self.data["candidate_factories"]

    def factory_log_seen(self, factory: str, log_key: str) -> bool:
        entry = self.data["candidate_factories"].get(factory.lower())
        return bool(entry) and log_key in entry["seen_log_keys"]

    def mark_factory_log_seen(self, factory: str, log_key: str, cap: int = 5000) -> None:
        with _LOCK:
            entry = self.data["candidate_factories"].get(factory.lower())
            if entry is None:
                return
            entry["seen_log_keys"].append(log_key)
            if len(entry["seen_log_keys"]) > cap:
                entry["seen_log_keys"] = entry["seen_log_keys"][-cap:]
            self.mark_dirty()

    # -- frontend differ -----------------------------------------------------
    def frontend(self) -> Dict[str, Any]:
        return self.data["frontend"]


def as_checklist(state: "State") -> List[str]:
    """One-glance status summary used by --status and the heartbeat."""
    data = state.data
    return [
        f"tracked CAs: {len(data['tracked'])}",
        f"api tokens seen: {len(data['api_seen_tokens'])}",
        f"candidate factories: {len(data['candidate_factories'])}",
        f"dev wallets baselined: "
        f"{sum(1 for w in data['dev_wallets'].values() if w.get('baselined'))}"
        f"/{len(data['dev_wallets']) or len(config.TEAM_WALLETS)}",
        f"frontend deployment: {data['frontend'].get('deployment_id') or 'not seen yet'}",
    ]
