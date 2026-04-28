"""pump.fun migration watcher.

A pump.fun token "graduates" once its bonding curve fills (~$69k
mcap, ~85 SOL bonded). Migration creates a Raydium pool, which is
the moment the token becomes tradeable on a real DEX with real
liquidity — historically a strong "buy now" inflection.

We poll pump.fun's public frontend API every couple of minutes for
recently-completed coins and alert on each new one we haven't yet
recorded.

If pump.fun's API ever blocks server requests behind Cloudflare
(observed for some endpoints), we degrade silently: ChainAnomaly
rows still get written from any other path and the watcher logs and
moves on.
"""

import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy.orm import Session

from backend import settings_cache
from backend.alerts.telegram_bot import fire_migration_alert
from backend.database import SessionLocal
from backend.models.alert import Alert
from backend.models.chain_anomaly import ChainAnomaly

logger = logging.getLogger(__name__)

PUMP_FUN_API = "https://frontend-api-v3.pump.fun"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://pump.fun",
    "Referer": "https://pump.fun/",
}


def _cfg(key, default):
    return settings_cache.get(key, default)


async def _fetch_completed(limit: int = 50) -> list[dict]:
    """Recent completed (graduated) coins, newest first."""
    url = f"{PUMP_FUN_API}/coins"
    params = {
        "offset": 0, "limit": limit,
        "sort": "last_trade_timestamp", "order": "DESC",
        "includeNsfw": "false", "complete": "true",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params, headers=HEADERS)
            if resp.status_code == 200:
                # Pump.fun sometimes returns a list; sometimes wrapped.
                data = resp.json()
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    return data.get("coins") or data.get("data") or []
            else:
                logger.warning(
                    f"pump.fun /coins {resp.status_code}: {resp.text[:200]}"
                )
    except Exception as e:
        logger.error(f"pump.fun fetch failed: {e}")
    return []


def _recently_alerted_mints(db: Session, hours: int) -> set[str]:
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    return {
        r[0] for r in db.query(Alert.contract_address)
        .filter(
            Alert.alert_type == "pump_migration",
            Alert.fired_at >= cutoff,
        ).all()
    }


async def run_migration_watcher() -> dict:
    if not _cfg("MIGRATION_TRACKER_ENABLED", True):
        return {"skipped": "disabled"}

    dedup_h = int(_cfg("MIGRATION_DEDUP_HOURS", 24 * 7))
    coins = await _fetch_completed(limit=50)
    if not coins:
        return {"checked": 0, "fired": 0}

    db = SessionLocal()
    fired = 0
    persisted = 0
    try:
        already = _recently_alerted_mints(db, dedup_h)
        # Also dedup against ChainAnomaly directly so we never double-
        # row a migration even if the alert was deleted.
        existing_hashes = {
            r[0] for r in db.query(ChainAnomaly.tx_hash)
            .filter(ChainAnomaly.source == "solana")
            .filter(ChainAnomaly.event_type == "pump_migration")
            .filter(ChainAnomaly.detected_at >= datetime.utcnow() - timedelta(days=14))
            .all()
        }

        for c in coins:
            try:
                mint = c.get("mint") or c.get("token_address")
                if not mint or mint in already:
                    continue
                synth_hash = f"pump_migration:{mint}"
                if synth_hash in existing_hashes:
                    continue

                symbol = c.get("symbol") or ""
                name = c.get("name") or ""
                created_ts = c.get("created_timestamp") or 0
                last_ts = c.get("last_trade_timestamp") or 0
                mc_usd = float(c.get("usd_market_cap") or 0)
                raydium_pool = c.get("raydium_pool") or None

                # Skip if the token isn't actually migrated (defensive)
                if not raydium_pool:
                    continue

                # Time-to-graduate (informational)
                if created_ts and last_ts:
                    seconds_alive = (last_ts - created_ts) / 1000.0
                else:
                    seconds_alive = None

                event = {
                    "mint": mint,
                    "symbol": symbol,
                    "name": name,
                    "mc_usd": mc_usd,
                    "raydium_pool": raydium_pool,
                    "seconds_alive": seconds_alive,
                    "image_uri": c.get("image_uri"),
                    "twitter": c.get("twitter"),
                    "telegram": c.get("telegram"),
                    "website": c.get("website"),
                }
                alert_id = await fire_migration_alert(event, db=db)
                if alert_id:
                    fired += 1
                    already.add(mint)

                # Persist as ChainAnomaly regardless of alert success
                try:
                    anom = ChainAnomaly(
                        source="solana", event_type="pump_migration",
                        coin=symbol or mint[:8], side="buy",
                        notional_usd=Decimal(str(round(mc_usd, 2))) if mc_usd else None,
                        tx_hash=synth_hash,
                        extra_json=json.dumps({
                            "mint": mint, "name": name,
                            "raydium_pool": raydium_pool,
                            "seconds_alive": seconds_alive,
                        }),
                        is_alerted=alert_id is not None,
                        alert_id=alert_id,
                    )
                    db.add(anom)
                    db.commit()
                    persisted += 1
                    existing_hashes.add(synth_hash)
                except Exception as e:
                    logger.error(f"migration persist failed: {e}")
                    db.rollback()
            except Exception as e:
                logger.error(f"migration coin parse error: {e}")
                continue
        return {
            "checked": len(coins), "fired": fired,
            "persisted": persisted,
        }
    finally:
        db.close()
