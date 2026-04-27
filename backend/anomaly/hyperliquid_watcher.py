"""Hyperliquid whale-trade watcher.

Polls Hyperliquid every minute. For each non-major coin, scans
recent trades for any single fill at or above HL_MIN_NOTIONAL_USD.
For each new whale fill:

  1. Persist as a ChainAnomaly row (dedup via tx hash).
  2. Look up the actor wallet's lifetime fill count to flag "fresh."
  3. Fire a Telegram alert through the standard pipeline (Alert table
     for dedup + outcome tracking, hourly cap, per-(coin,side) dedup).

Same plumbing pattern as the cabal alert dispatcher — settings
editable from the Settings page, Telegram delivery best-effort,
hourly cap protects against floods.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend import settings_cache
from backend.alerts.telegram_bot import fire_hl_whale_alert
from backend.clients import hyperliquid
from backend.database import SessionLocal
from backend.models.alert import Alert
from backend.models.chain_anomaly import ChainAnomaly
from backend.models.major_coin import MajorCoin

logger = logging.getLogger(__name__)


# How many recentTrades calls to fan out at once. Hyperliquid is generous
# but no need to thrash the endpoint.
HL_FETCH_CONCURRENCY = 10

# In-memory cache of wallet -> fill count. Whale wallets don't go from
# fresh to veteran inside an hour; saves repeated userFills calls.
_FILL_COUNT_CACHE: dict[str, tuple[float, int]] = {}
_FILL_CACHE_TTL_SEC = 60 * 60


def _cfg(key: str, default):
    return settings_cache.get(key, default)


async def _cached_fill_count(addr: str) -> int:
    import time
    if not addr:
        return 0
    entry = _FILL_COUNT_CACHE.get(addr)
    if entry and (time.time() - entry[0] < _FILL_CACHE_TTL_SEC):
        return entry[1]
    count = await hyperliquid.user_fill_count(addr)
    _FILL_COUNT_CACHE[addr] = (time.time(), count)
    return count


def _recently_alerted_keys(db: Session, dedup_min: int) -> set[str]:
    """Return set of (coin|side) keys we've already alerted within the
    dedup window. Same coin + side won't re-fire for `dedup_min` minutes
    even if more $1M trades come in."""
    since = datetime.utcnow() - timedelta(minutes=dedup_min)
    rows = (
        db.query(Alert.contract_address, Alert.trigger_reason)
        .filter(
            Alert.alert_type == "hl_whale_trade",
            Alert.fired_at >= since,
        )
        .all()
    )
    # We pack (coin|side) into contract_address for dedup since the alert
    # isn't really tied to a token CA — see fire_hl_whale_alert.
    return {r[0] for r in rows if r[0]}


def _alerts_sent_last_hour(db: Session) -> int:
    cutoff = datetime.utcnow() - timedelta(hours=1)
    return (
        db.query(func.count(Alert.id))
        .filter(
            Alert.alert_type == "hl_whale_trade",
            Alert.fired_at >= cutoff,
        )
        .scalar() or 0
    )


async def run_hyperliquid_watcher() -> dict:
    if not _cfg("HL_ENABLED", True):
        return {"skipped": "disabled"}

    threshold = float(_cfg("HL_MIN_NOTIONAL_USD", 1_000_000))
    fresh_max = int(_cfg("HL_FRESH_WALLET_MAX_FILLS", 5))
    dedup_min = int(_cfg("HL_DEDUP_WINDOW_MIN", 30))
    hourly_cap = int(_cfg("HL_MAX_ALERTS_PER_HOUR", 8))

    universe, ctxs = await hyperliquid.meta_and_ctxs()
    if not universe:
        return {"error": "no_meta"}
    ctx_by_coin = {}
    for u, c in zip(universe, ctxs):
        name = u.get("name")
        if name:
            ctx_by_coin[name] = c or {}

    db = SessionLocal()
    new_anomalies = 0
    fired = 0
    try:
        majors = {
            (s or "").upper()
            for (s,) in db.query(MajorCoin.symbol)
            .filter(MajorCoin.excluded.is_(True)).all()
        }

        coins_to_check = [
            u["name"] for u in universe
            if u.get("name") and not u.get("isDelisted", False)
            and u["name"].upper() not in majors
        ]
        if not coins_to_check:
            return {"checked": 0}

        sem = asyncio.Semaphore(HL_FETCH_CONCURRENCY)

        async def _fetch(coin: str):
            async with sem:
                trades = await hyperliquid.recent_trades(coin)
                return coin, trades

        results = await asyncio.gather(
            *[_fetch(c) for c in coins_to_check], return_exceptions=True
        )

        # Pull existing tx hashes for THIS source so we can dedup the
        # raw event log cheaply (separate from alert dedup).
        # Limit to recent to keep the lookup small.
        since_anom = datetime.utcnow() - timedelta(hours=2)
        existing_hashes: set[str] = {
            r[0] for r in db.query(ChainAnomaly.tx_hash)
            .filter(ChainAnomaly.source == "hyperliquid")
            .filter(ChainAnomaly.detected_at >= since_anom)
            .all()
        }

        # First pass: persist all new whale candidates as ChainAnomaly
        # rows so the dashboard/feed has them even if the alert is
        # deduped or capped.
        candidates: list[dict] = []
        for result in results:
            if isinstance(result, Exception):
                continue
            coin, trades = result
            if not trades:
                continue
            for t in trades:
                try:
                    px = float(t.get("px") or 0)
                    sz = float(t.get("sz") or 0)
                    if px <= 0 or sz <= 0:
                        continue
                    notional = px * sz
                    if notional < threshold:
                        continue
                    h = t.get("hash") or ""
                    if not h or h in existing_hashes:
                        continue
                    side_raw = t.get("side")  # "B" = buy/long, "A" = sell/short
                    side = "long" if side_raw == "B" else "short" if side_raw == "A" else "unknown"
                    users = t.get("users") or []
                    actor = users[0] if users else None
                    counterparty = users[1] if len(users) > 1 else None
                    candidates.append({
                        "coin": coin, "side": side, "side_raw": side_raw,
                        "px": px, "sz": sz, "notional": notional,
                        "hash": h, "time_ms": t.get("time"),
                        "actor": actor, "counterparty": counterparty,
                    })
                    existing_hashes.add(h)
                except Exception as e:
                    logger.warning(f"HL trade parse error on {coin}: {e}")

        if not candidates:
            return {"checked": len(coins_to_check), "candidates": 0}

        # Look up fill counts for the actor wallets (cached, parallel)
        actor_addrs = list({c["actor"] for c in candidates if c["actor"]})

        async def _bounded(addr):
            async with sem:
                return addr, await _cached_fill_count(addr)
        addr_counts = await asyncio.gather(
            *[_bounded(a) for a in actor_addrs], return_exceptions=True
        )
        count_by_addr = {
            r[0]: r[1] for r in addr_counts if not isinstance(r, Exception)
        }

        # Persist anomalies
        for c in candidates:
            try:
                actor = c["actor"]
                fill_count = count_by_addr.get(actor, 0)
                is_fresh = fill_count <= fresh_max
                anomaly = ChainAnomaly(
                    source="hyperliquid",
                    event_type="whale_trade",
                    coin=c["coin"], side=c["side"],
                    notional_usd=Decimal(str(round(c["notional"], 2))),
                    px=Decimal(str(c["px"])), sz=Decimal(str(c["sz"])),
                    actor_address=actor,
                    counterparty_address=c["counterparty"],
                    is_fresh_wallet=is_fresh,
                    actor_history_count=fill_count,
                    tx_hash=c["hash"],
                    extra_json=json.dumps({
                        "side_raw": c["side_raw"], "time_ms": c["time_ms"],
                    }),
                    detected_at=(
                        datetime.utcfromtimestamp(c["time_ms"] / 1000)
                        if c["time_ms"] else datetime.utcnow()
                    ),
                )
                db.add(anomaly)
                db.commit()
                new_anomalies += 1
                c["is_fresh"] = is_fresh
                c["fill_count"] = fill_count
                c["anomaly_id"] = anomaly.id
            except Exception as e:
                db.rollback()
                logger.error(f"HL anomaly persist error: {e}")

        # Alert dispatch — apply per-(coin,side) dedup + hourly cap.
        # Sort by notional desc so the biggest ones get the budget.
        candidates.sort(key=lambda c: -c["notional"])
        already_keys = _recently_alerted_keys(db, dedup_min)
        budget = max(0, hourly_cap - _alerts_sent_last_hour(db))

        for c in candidates:
            if budget <= 0:
                break
            key = f"hl|{c['coin']}|{c['side']}"
            if key in already_keys:
                continue
            ctx = ctx_by_coin.get(c["coin"]) or {}
            event = {
                "coin": c["coin"], "side": c["side"],
                "notional_usd": c["notional"], "px": c["px"], "sz": c["sz"],
                "actor": c["actor"], "fill_count": c.get("fill_count", 0),
                "is_fresh": c.get("is_fresh", False),
                "tx_hash": c["hash"],
                "mark_px": float(ctx.get("markPx") or 0),
                "open_interest": float(ctx.get("openInterest") or 0),
                "day_volume": float(ctx.get("dayNtlVlm") or 0),
                "funding": float(ctx.get("funding") or 0),
                "dedup_key": key,
            }
            alert_id = await fire_hl_whale_alert(event, db=db)
            if alert_id:
                fired += 1
                budget -= 1
                already_keys.add(key)
                # Link the anomaly to the alert
                try:
                    anom = db.query(ChainAnomaly).filter_by(
                        id=c.get("anomaly_id")
                    ).first()
                    if anom:
                        anom.is_alerted = True
                        anom.alert_id = alert_id
                        db.commit()
                except Exception:
                    db.rollback()

        if new_anomalies or fired:
            logger.info(
                f"hyperliquid_watcher: {new_anomalies} new whale anomalies, "
                f"{fired} alerts fired (budget left {budget})"
            )
        return {
            "checked_coins": len(coins_to_check),
            "candidates": len(candidates),
            "new_anomalies": new_anomalies,
            "alerts_fired": fired,
        }
    finally:
        db.close()
