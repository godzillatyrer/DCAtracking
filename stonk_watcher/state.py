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

SCHEMA_VERSION = 2


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
        "lp_locks": {},                   # locker addr -> block cursor
        "lp_lock_seen": [],               # dedupe for lock events
        "liquidity_last_scanned": None,   # cursor for tracked-token LP watch
        "token_pools": {},                # token -> [pool addresses seen]
        "lp_alerted": [],                 # tokens whose LP alert already fired
        # Track 3
        "frontend": {
            "deployment_id": None,
            "baselined": False,           # first run records silently
            "seen_addresses": [],
            "negative_cache": [],         # addresses that 404 on Blockscout
        },
        "meta": {"created_at": time.time(), "schema": SCHEMA_VERSION},
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
            self._migrate()

    SCHEMA_VERSION = SCHEMA_VERSION  # class alias for callers/tests

    def _migrate(self) -> None:
        """One-time cleanups for state written by earlier versions.

        Records what it removed in meta["migration_v2"] so the watcher can
        report it — a silent purge could otherwise disarm the real launcher
        factory with nobody the wiser.
        """
        meta = self.data.setdefault("meta", {})
        if meta.get("schema", 1) >= self.SCHEMA_VERSION:
            return
        # v2: the chain-wide mint watch was removed. Tokens it tracked
        # (source "mint") are NOT launchpad tokens, and factories armed
        # before per-entry provenance existed cannot be told apart from the
        # generic memecoin factories that incident learned — so they go too.
        tracked = self.data.get("tracked", {})
        factories = self.data.get("candidate_factories", {})

        purged_factories = [a for a, f in factories.items() if "source" not in f]
        for addr in purged_factories:
            del factories[addr]

        # Mint leftovers, plus tokens confirmed *by* a factory we just
        # purged — those were the incident's false positives and would keep
        # firing LP/market alerts from the AMM scan.
        doomed = {c for c, i in tracked.items() if i.get("source") == "mint"}
        doomed |= {c for c, i in tracked.items()
                   if i.get("source") == "chain"
                   and str(i.get("factory", "")).lower() in purged_factories}
        for ca in doomed:
            del tracked[ca]

        # Critical: pre-v2 code stamped creator_resolved on every tracked
        # token, including API-listed ones, while arming factories without
        # provenance. Purging those factories above would otherwise leave
        # the real launcher factory unarmed AND unlearnable, because the
        # token that could re-teach it is marked already-resolved. Clearing
        # the flag makes the next cycle re-learn it.
        for info in tracked.values():
            if info.get("source") == "api":
                info.pop("creator_resolved", None)

        for stale_key in ("mint_last_scanned", "mint_seen_tokens"):
            self.data.pop(stale_key, None)
        meta["schema"] = self.SCHEMA_VERSION
        meta["migration_v2"] = {
            "purged_factories": purged_factories,
            "purged_tokens": sorted(doomed),
            "reported": False,
        }
        self.mark_dirty()

    def set_tracked_field(self, ca: str, key: str, value: Any) -> None:
        """Mutate a tracked entry under the lock.

        Inserting a NEW key into a nested dict without the lock can crash a
        concurrent save() with "dictionary changed size during iteration",
        because json.dump walks these dicts incrementally.
        """
        with _LOCK:
            entry = self.data["tracked"].get(ca.lower())
            if entry is not None:
                entry[key] = value
                self.mark_dirty()

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
    def add_candidate_factory(self, addr: str, deploy_block: int,
                              source: str) -> bool:
        """source records the provenance that justified arming this factory:
        "team_wallet" (watched wallet deployed it), "config"
        (WATCH_CONTRACTS), or "learned_from_api" (creator of an API-listed
        launchpad token). Anything armed without a recorded source is
        untrusted and purged by migration."""
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
                "source": source,
            }
            self.mark_dirty()
            return True

    def set_factory_field(self, addr: str, key: str, value: Any) -> None:
        with _LOCK:
            entry = self.data["candidate_factories"].get(addr.lower())
            if entry is not None:
                entry[key] = value
                self.mark_dirty()

    def is_known_launcher_factory(self, addr: str) -> bool:
        return addr.lower() in self.data["candidate_factories"]

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

    def take_migration_report(self) -> Optional[Dict[str, Any]]:
        """Return the v2 migration summary once, then mark it reported."""
        with _LOCK:
            report = self.data.get("meta", {}).get("migration_v2")
            if not report or report.get("reported"):
                return None
            report["reported"] = True
            self.mark_dirty()
            return report


def as_checklist(state: "State") -> List[str]:
    """One-glance status summary used by --status and the heartbeat.

    Runs on the watchdog/heartbeat threads while workers mutate state, so it
    takes the lock: a first-sight wallet inserting into dev_wallets mid-scan
    would otherwise raise "dictionary changed size during iteration" and
    kill the heartbeat — the very signal that proves the watcher is alive.
    """
    with _LOCK:
        return _checklist_locked(state)


def _checklist_locked(state: "State") -> List[str]:
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
