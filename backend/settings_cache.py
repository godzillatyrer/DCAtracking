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

    # Hyperliquid whale watcher
    ("HL_ENABLED", "hyperliquid", True, "bool",
     "Master switch for the Hyperliquid whale-trade watcher."),
    ("HL_MIN_NOTIONAL_USD", "hyperliquid", 1_000_000.0, "float",
     "Minimum single-trade notional to qualify as a whale trade."),
    ("HL_FRESH_WALLET_MAX_FILLS", "hyperliquid", 5, "int",
     "Wallets with this many or fewer total Hyperliquid fills get the FRESH badge."),
    ("HL_DEDUP_WINDOW_MIN", "hyperliquid", 30, "int",
     "Don't re-alert the same (coin, side) within this many minutes."),
    ("HL_MAX_ALERTS_PER_HOUR", "hyperliquid", 8, "int",
     "Global hourly cap on Hyperliquid whale alerts."),
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
