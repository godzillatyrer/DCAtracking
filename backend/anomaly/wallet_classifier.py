"""Cheap fresh/dormant classifier for arbitrary Solana wallets.

For each input address:
  - Pull the last N signatures via Helius (one RPC).
  - Classify:
      fresh   = signature count <= FRESH_MAX_TXS  AND
                first signature seen within FRESH_MAX_AGE_DAYS
      dormant = signature count high (> 5)        AND
                most-recent activity BEFORE the current buy was
                older than DORMANT_MIN_INACTIVE_DAYS
  - Cache the result in `wallet_classifications` for CACHE_TTL_HOURS
    so we don't re-RPC the same wallet on every cycle.

Freshness changes slowly; dormancy too. The cache is the difference
between "this is feasible" and "we melt our Helius credits."
"""

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend import settings_cache
from backend.clients import helius
from backend.database import SessionLocal
from backend.models.wallet_classification import WalletClassification

logger = logging.getLogger(__name__)

# How many recent signatures to inspect — capped low because cost
# scales with 1 RPC per wallet.
SIG_LIMIT = 50


def _cfg(key, default):
    return settings_cache.get(key, default)


async def _fetch_signatures(addr: str) -> list[dict]:
    if not helius.configured or not addr:
        return []
    return await helius.get_signatures(addr, limit=SIG_LIMIT)


def _classify_from_sigs(
    sigs: list[dict],
    *,
    fresh_max_txs: int,
    fresh_max_age_days: int,
    dormant_min_inactive_days: int,
    now: datetime,
) -> dict:
    """Pure function — classifies given a sigs list. Easy to unit-test."""
    out = {
        "is_fresh": False,
        "is_dormant": False,
        "tx_count_observed": len(sigs),
        "first_seen_at": None,
        "last_active_at": None,
    }
    if not sigs:
        # No signatures at all = brand new wallet → fresh by definition
        out["is_fresh"] = True
        return out

    # blockTime can be missing on some RPC responses. Guard.
    times = [
        datetime.utcfromtimestamp(s["blockTime"])
        for s in sigs
        if s.get("blockTime")
    ]
    if not times:
        # Got sigs but no times — degrade: count alone determines fresh
        out["is_fresh"] = len(sigs) <= fresh_max_txs
        return out

    times.sort()
    out["first_seen_at"] = times[0]
    out["last_active_at"] = times[-1]

    # Fresh = few txs AND not too old
    if (
        len(sigs) <= fresh_max_txs
        and times[0] >= now - timedelta(days=fresh_max_age_days)
    ):
        out["is_fresh"] = True

    # Dormant = enough history AND most-recent-before-now happened a long
    # time ago. We use the SECOND-most-recent timestamp because the most
    # recent might BE the buy that triggered classification.
    if len(sigs) > 5 and len(times) >= 2:
        prev_active = times[-2]
        if prev_active < now - timedelta(days=dormant_min_inactive_days):
            out["is_dormant"] = True

    return out


async def classify_wallet(addr: str, db: Session) -> dict:
    """Cached. Returns dict with is_fresh, is_dormant, etc."""
    if not addr:
        return {"is_fresh": False, "is_dormant": False}

    now = datetime.utcnow()
    cache_ttl = int(_cfg("WALLET_CLASSIFY_CACHE_TTL_HOURS", 24))

    cached = db.query(WalletClassification).filter_by(wallet_address=addr).first()
    if cached and cached.expires_at and cached.expires_at > now:
        return {
            "is_fresh": cached.is_fresh,
            "is_dormant": cached.is_dormant,
            "tx_count_observed": cached.tx_count_observed,
            "first_seen_at": cached.first_seen_at,
            "last_active_at": cached.last_active_at,
        }

    fresh_max_txs = int(_cfg("WALLET_CLASSIFY_FRESH_MAX_TXS", 20))
    fresh_max_age = int(_cfg("WALLET_CLASSIFY_FRESH_MAX_AGE_DAYS", 7))
    dormant_min = int(_cfg("DORMANT_SWARM_MIN_INACTIVE_DAYS", 21))

    sigs = await _fetch_signatures(addr)
    cls = _classify_from_sigs(
        sigs,
        fresh_max_txs=fresh_max_txs,
        fresh_max_age_days=fresh_max_age,
        dormant_min_inactive_days=dormant_min,
        now=now,
    )

    expires = now + timedelta(hours=cache_ttl)
    if cached is None:
        cached = WalletClassification(wallet_address=addr)
        db.add(cached)
    cached.chain = "solana"
    cached.is_fresh = cls["is_fresh"]
    cached.is_dormant = cls["is_dormant"]
    cached.tx_count_observed = cls["tx_count_observed"]
    cached.first_seen_at = cls.get("first_seen_at")
    cached.last_active_at = cls.get("last_active_at")
    cached.classified_at = now
    cached.expires_at = expires
    try:
        db.commit()
    except Exception as e:
        logger.warning(f"classify cache write failed for {addr[:8]}: {e}")
        db.rollback()
    return cls


async def classify_many(addrs: list[str], db: Session,
                        concurrency: int = 10) -> dict[str, dict]:
    """Batch classifier. Returns {addr: classification}."""
    sem = asyncio.Semaphore(concurrency)

    async def _one(addr: str):
        async with sem:
            return addr, await classify_wallet(addr, db)

    pairs = await asyncio.gather(*[_one(a) for a in addrs],
                                 return_exceptions=True)
    out = {}
    for r in pairs:
        if isinstance(r, Exception):
            continue
        addr, cls = r
        out[addr] = cls
    return out
