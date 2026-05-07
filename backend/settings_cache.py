"""Runtime settings cache.

- First call lazily bootstraps DEFAULTS into the `app_settings` table.
- In-process cache refreshed every CACHE_TTL seconds.
- `get(key, fallback)` returns the current value (or fallback if the
  key isn't known / can't be parsed).

Intentionally simple — this is for operator-visible knobs, not secrets.
"""

import json
import logging
import threading
import time
from typing import Any

from backend.database import SessionLocal
from backend.models.app_setting import AppSetting

logger = logging.getLogger(__name__)

CACHE_TTL = 30.0  # seconds

_lock = threading.Lock()
_cache: dict[str, Any] = {}
_cache_last_load: float = 0.0
_bootstrapped: bool = False


# ─── Default catalog ─────────────────────────────────────────────────
# (key, category, default, type, description)

DEFAULTS: list[tuple[str, str, Any, str, str]] = [
    # Master switches
    ("ALERTS_PAUSED", "alerts", False, "bool",
     "When ON, no watcher Telegram alerts are sent (anomalies still log and dedup). /pause and /resume commands toggle this."),

    # Convergence alerts
    ("SAME_ENTITY_MIN_WALLETS", "alerts", 3, "int",
     "Minimum wallets from the same funder cluster required to trigger a convergence alert."),
    ("CONVERGENCE_WINDOW_MIN", "alerts", 60, "int",
     "Window (minutes) of activity considered when grouping buys into a convergence."),
    ("FRESH_MAX_MC_USD", "alerts", 50_000.0, "float",
     "Only alert on coins with DexScreener MC below this (USD). New pump.fun coins with no pair data always pass."),
    ("MAX_ALERTS_PER_HOUR", "alerts", 5, "int",
     "Global hourly cap on convergence alerts to Telegram."),

    # Sniper solo alerts
    ("SOLO_ALERTS_ENABLED", "sniper", True, "bool",
     "Master switch for 🎯 Sniper solo alerts."),
    ("SOLO_MAX_MC_USD", "sniper", 150_000.0, "float",
     "MC ceiling for sniper solo alerts. Looser than convergence since snipers often buy right at migration."),
    ("SOLO_MAX_ALERTS_PER_HOUR", "sniper", 3, "int",
     "Hourly cap on sniper solo alerts."),
    ("SNIPER_MAX_AVG_ENTRY_USD", "sniper", 500.0, "float",
     "Classification threshold: max average entry size for the sniper profile."),
    ("SNIPER_MIN_EXIT_MULTIPLIER", "sniper", 3.0, "float",
     "Classification threshold: minimum mean exit multiplier."),
    ("SNIPER_MIN_WIN_RATE", "sniper", 0.60, "float",
     "Classification threshold: minimum win rate on closed positions (0–1)."),
    ("SNIPER_MIN_CLOSED_POSITIONS", "sniper", 4, "int",
     "Classification threshold: minimum closed positions (anti-lucky-twice sample size)."),
    ("SNIPER_MIN_NET_PROFIT_USD", "sniper", 1000.0, "float",
     "Classification threshold: minimum lifetime net PnL."),

    # Exit alerts
    ("EXIT_ALERTS_ENABLED", "exit", True, "bool",
     "Fire Telegram alert when tracked wallets from the same cluster dump a mint."),
    ("EXIT_MIN_WALLETS", "exit", 3, "int",
     "Minimum same-entity wallets selling within the window to trigger an exit alert."),
    ("EXIT_WINDOW_MIN", "exit", 10, "int",
     "Window (minutes) over which to correlate sells."),

    # Wallet decay
    ("WALLET_DORMANT_DAYS", "decay", 14, "int",
     "Wallets inactive this many days are marked dormant and de-weighted in alerts."),

    # Behavioral clustering
    ("BEHAVIOR_MIN_SHARED_MINTS", "clustering", 5, "int",
     "Minimum mints two wallets must have both bought (within window) before behavior-merging their entities."),
    ("BEHAVIOR_TIME_WINDOW_SEC", "clustering", 120, "int",
     "Seconds: two wallets' buys on the same mint count as 'together' if within this window."),

    # Wallet classifier (used by graph walk + DCA watcher)
    ("WALLET_CLASSIFY_FRESH_MAX_TXS", "classifier", 20, "int",
     "Max signatures a wallet can have to be classified 'fresh'."),
    ("WALLET_CLASSIFY_FRESH_MAX_AGE_DAYS", "classifier", 7, "int",
     "Wallet's first signature must be no older than this to be 'fresh'."),
    ("WALLET_CLASSIFY_CACHE_TTL_HOURS", "classifier", 24, "int",
     "How long to cache a wallet's fresh/dormant classification."),
    ("DORMANT_MIN_INACTIVE_DAYS", "classifier", 21, "int",
     "Wallet must have been inactive at least this many days before the buy to count as dormant."),

    # ─ Internal state (hidden from Settings UI; category prefix _) ─
    ("_LAST_TG_UPDATE_ID", "_internal", 0, "int",
     "Internal: last Telegram update_id processed by the command poller."),

    # Jupiter DCA order watcher
    ("DCA_ORDER_ENABLED", "dca", True, "bool",
     "Master switch for Jupiter DCA large-order alerts. Monitors the Jupiter DCA program for openDca/openDcaV2 instructions above the value threshold on low-cap tokens."),
    ("DCA_ORDER_MIN_VALUE_USD", "dca", 150_000.0, "float",
     "Minimum total DCA order value (USD) to trigger an alert. Only orders above this fire."),
    ("DCA_ORDER_MAX_MC_USD", "dca", 50_000_000.0, "float",
     "Only alert on tokens with market cap below this (USD). Tokens with no pair data always pass."),
    ("DCA_ORDER_DEDUP_HOURS", "dca", 24, "int",
     "Per-mint dedup window for DCA order alerts. Same output token won't re-alert within this window."),
    ("DCA_ORDER_MAX_ALERTS_PER_HOUR", "dca", 3, "int",
     "Hourly cap on DCA order alerts to Telegram."),

    # CEX-funded accumulation pipeline (the new core detector)
    ("CEX_HARVEST_ENABLED", "cex_accum", True, "bool",
     "Master switch for the CEX outflow harvester. Polls CEX hot wallets for outflows and adds new recipients as cex_funded tracked wallets."),
    ("CEX_HARVEST_SIGS_PER_HOT", "cex_accum", 80, "int",
     "Number of recent signatures to scan per CEX hot wallet per cycle."),
    ("CEX_HARVEST_MIN_SOL", "cex_accum", 1.0, "float",
     "Minimum SOL transfer size to count as a fundable outflow (filters dust). 1 SOL is the smallest realistic insider funding amount."),
    ("CEX_HARVEST_MAX_SOL", "cex_accum", 5000.0, "float",
     "Skip outflows larger than this — likely OTC desks or treasury, not insiders."),
    ("CEX_HARVEST_TARGET_EXCHANGES", "cex_accum",
     "binance,okx,bybit,bitget", "str",
     "Comma-separated list of CEX names (matched against cex_addresses.exchange) whose outflows we harvest. Lower-priority exchanges can be added at runtime."),

    ("CEX_ACCUMULATION_ENABLED", "cex_accum", True, "bool",
     "Master switch for the CEX-funded accumulation alerter."),
    ("CEX_ACCUMULATION_MIN_WALLETS", "cex_accum", 3, "int",
     "Minimum number of CEX-funded wallets net-long on the same mint to trigger an alert."),
    ("CEX_ACCUMULATION_MIN_SUPPLY_PCT", "cex_accum", 1.5, "float",
     "Minimum cumulative percentage of token supply held by the wallet group. Below this the accumulation isn't meaningful."),
    ("CEX_ACCUMULATION_MIN_NET_USD_PER_WALLET", "cex_accum", 500.0, "float",
     "Each wallet must have spent at least this in net buys (buy USD - sell USD) on the mint to be counted. Filters drive-by pump.fun apes."),
    ("CEX_ACCUMULATION_MIN_HOLD_HOURS", "cex_accum", 24.0, "float",
     "Earliest buy must be at least this many hours ago. Stealth accumulation, not same-day apes."),
    ("CEX_ACCUMULATION_MAX_SELL_RATIO", "cex_accum", 0.30, "float",
     "Wallet group's total sell USD / total buy USD must be at most this. Above this means they're flipping, not accumulating."),
    ("CEX_ACCUMULATION_MIN_MC_USD", "cex_accum", 500_000.0, "float",
     "Token MC floor. Below $500k is pump.fun noise."),
    ("CEX_ACCUMULATION_MAX_MC_USD", "cex_accum", 20_000_000.0, "float",
     "Token MC ceiling. Above $20M the insider edge is mostly priced in."),
    ("CEX_ACCUMULATION_DEDUP_HOURS", "cex_accum", 168, "int",
     "Per-mint dedup window. Default 7 days — once we've alerted, don't spam."),
    ("CEX_ACCUMULATION_MAX_ALERTS_PER_HOUR", "cex_accum", 4, "int",
     "Global hourly cap on CEX accumulation alerts."),
]


# ─── Internal ─────────────────────────────────────────────────────────

def _parse(value_json: str, value_type: str) -> Any:
    try:
        raw = json.loads(value_json)
    except Exception:
        return None
    if value_type == "int":
        return int(raw)
    if value_type == "float":
        return float(raw)
    if value_type == "bool":
        return bool(raw)
    return raw


def _bootstrap(db) -> None:
    """Seed DEFAULTS into the DB for any key that's missing. Idempotent."""
    existing = {row.key for row in db.query(AppSetting.key).all()}
    to_add = []
    for key, category, default, vtype, desc in DEFAULTS:
        if key in existing:
            continue
        to_add.append(AppSetting(
            key=key,
            value_json=json.dumps(default),
            value_type=vtype,
            category=category,
            description=desc,
            default_json=json.dumps(default),
        ))
    if to_add:
        db.add_all(to_add)
        db.commit()
        logger.info(f"settings_cache: seeded {len(to_add)} default settings")


def _load(force: bool = False) -> None:
    global _cache, _cache_last_load, _bootstrapped
    now = time.time()
    if not force and _bootstrapped and (now - _cache_last_load) < CACHE_TTL:
        return
    with _lock:
        if not force and _bootstrapped and (time.time() - _cache_last_load) < CACHE_TTL:
            return
        db = SessionLocal()
        try:
            if not _bootstrapped:
                try:
                    _bootstrap(db)
                except Exception as e:
                    logger.warning(f"settings_cache bootstrap skipped: {e}")
                _bootstrapped = True
            fresh: dict[str, Any] = {}
            for row in db.query(AppSetting).all():
                parsed = _parse(row.value_json, row.value_type)
                if parsed is not None:
                    fresh[row.key] = parsed
            _cache = fresh
            _cache_last_load = time.time()
        finally:
            db.close()


# ─── Public API ───────────────────────────────────────────────────────

def get(key: str, fallback: Any) -> Any:
    try:
        _load()
    except Exception as e:
        logger.warning(f"settings_cache load failed, using fallback: {e}")
        return fallback
    return _cache.get(key, fallback)


def set_value(key: str, raw: Any) -> dict:
    """Persist a new value; type-coerce against the registered default.
    Returns the stored row as a dict."""
    db = SessionLocal()
    try:
        row = db.query(AppSetting).filter_by(key=key).first()
        if row is None:
            # Honour DEFAULTS for first-write-without-bootstrap paths
            for k, category, default, vtype, desc in DEFAULTS:
                if k == key:
                    row = AppSetting(
                        key=key, category=category, value_type=vtype,
                        description=desc, default_json=json.dumps(default),
                        value_json=json.dumps(default),
                    )
                    db.add(row)
                    break
            else:
                raise KeyError(f"unknown setting: {key}")

        # Coerce
        if row.value_type == "int":
            coerced = int(float(raw))
        elif row.value_type == "float":
            coerced = float(raw)
        elif row.value_type == "bool":
            coerced = bool(raw) if isinstance(raw, bool) else (
                str(raw).lower() in ("1", "true", "yes", "on")
            )
        else:
            coerced = str(raw)
        row.value_json = json.dumps(coerced)
        db.commit()
        _load(force=True)
        return {
            "key": row.key, "value": coerced, "category": row.category,
            "type": row.value_type, "description": row.description,
        }
    finally:
        db.close()


def list_all() -> list[dict]:
    """Return every known setting, current + default, for the UI."""
    _load()
    db = SessionLocal()
    try:
        rows = db.query(AppSetting).order_by(AppSetting.category, AppSetting.key).all()
        return [
            {
                "key": r.key,
                "category": r.category,
                "type": r.value_type,
                "description": r.description,
                "value": _parse(r.value_json, r.value_type),
                "default": _parse(r.default_json or r.value_json, r.value_type),
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]
    finally:
        db.close()


def reset_default(key: str) -> dict | None:
    db = SessionLocal()
    try:
        row = db.query(AppSetting).filter_by(key=key).first()
        if row is None or not row.default_json:
            return None
        row.value_json = row.default_json
        db.commit()
        _load(force=True)
        return {"key": key, "value": _parse(row.value_json, row.value_type)}
    finally:
        db.close()
