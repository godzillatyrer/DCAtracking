"""Cheap fresh/dormant classifier — works on Solana, Ethereum, BSC.

For each input (address, chain):
  - Pull the last N transactions/signatures (one explorer call).
  - Classify:
      fresh   = tx count <= FRESH_MAX_TXS AND first tx within
                FRESH_MAX_AGE_DAYS
      dormant = tx count high (> 5) AND second-most-recent activity
                older than DORMANT_MIN_INACTIVE_DAYS (catches the
                "wallet woke up to buy" case; the most recent tx is
                often the buy that triggered classification)
  - Cache in `wallet_classifications` (chain-qualified) for
    CACHE_TTL_HOURS. Fresh→active is a slow transition — 24h cache
    is the difference between "feasible" and "we melt our RPC credits."
"""

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend import settings_cache
from backend.clients import evm_explorer, helius
from backend.database import SessionLocal
from backend.models.wallet_classification import WalletClassification

logger = logging.getLogger(__name__)

# How many recent signatures to inspect — capped low because cost
# scales with 1 RPC per wallet.
SIG_LIMIT = 50


def _cfg(key, default):
    return settings_cache.get(key, default)


async def _fetch_timestamps(addr: str, chain: str) -> list[datetime]:
    """Returns recent activity timestamps for the wallet, newest first.
    Chain-agnostic: dispatches to the right explorer. Empty list on
    any failure or missing config.
    """
    if not addr:
        return []
    if chain == "solana":
        if not helius.configured:
            return []
        sigs = await helius.get_signatures(addr, limit=SIG_LIMIT)
        return [
            datetime.utcfromtimestamp(s["blockTime"])
            for s in (sigs or [])
            if s.get("blockTime")
        ]
    if chain in ("eth", "bsc"):
        if not evm_explorer.configured_for(chain):
            return []
        txs = await evm_explorer.get_recent_txs(addr, chain, limit=SIG_LIMIT)
        out = []
        for t in txs:
            ts = t.get("timeStamp")
            if not ts:
                continue
            try:
                out.append(datetime.utcfromtimestamp(int(ts)))
            except Exception:
                continue
        return out
    return []


def _classify_from_sigs(
    sigs: list[dict] | None = None,
    *,
    fresh_max_txs: int,
    fresh_max_age_days: int,
    dormant_min_inactive_days: int,
    now: datetime,
    timestamps: list[datetime] | None = None,
) -> dict:
    """Pure function — classifies given a list of activity timestamps.

    Accepts EITHER a list of timestamps directly (preferred) OR a
    list of dicts with 'blockTime' fields (legacy Solana shape) so
    existing tests still pass.
    """
    if timestamps is None:
        sigs = sigs or []
        timestamps = [
            datetime.utcfromtimestamp(s["blockTime"])
            for s in sigs
            if isinstance(s, dict) and s.get("blockTime")
        ]
    times = list(timestamps or [])

    out = {
        "is_fresh": False,
        "is_dormant": False,
        "tx_count_observed": len(times) if times else (len(sigs) if sigs else 0),
        "first_seen_at": None,
        "last_active_at": None,
    }
    if not times:
        # Either no activity (truly new), or rate-limited / no key.
        # Treat as fresh — caller may want to suppress this case if
        # they care about the difference.
        out["is_fresh"] = True
        return out

    times.sort()  # ascending
    out["first_seen_at"] = times[0]
    out["last_active_at"] = times[-1]

    if (
        len(times) <= fresh_max_txs
        and times[0] >= now - timedelta(days=fresh_max_age_days)
    ):
        out["is_fresh"] = True

    # Dormant = some history + the SECOND-most-recent activity was a
    # while ago. Most-recent is often the buy that just triggered us.
    if len(times) > 5 and len(times) >= 2:
        prev_active = times[-2]
        if prev_active < now - timedelta(days=dormant_min_inactive_days):
            out["is_dormant"] = True

    return out


async def classify_wallet(addr: str, db: Session, chain: str = "solana") -> dict:
    """Cached, chain-aware. Returns dict with is_fresh, is_dormant, ..."""
    if not addr:
        return {"is_fresh": False, "is_dormant": False}

    # EVM addresses are case-insensitive; normalize for stable cache keys.
    cache_key = addr.lower() if chain in ("eth", "bsc") else addr

    now = datetime.utcnow()
    cache_ttl = int(_cfg("WALLET_CLASSIFY_CACHE_TTL_HOURS", 24))

    cached = db.query(WalletClassification).filter_by(
        wallet_address=cache_key, chain=chain,
    ).first()
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

    timestamps = await _fetch_timestamps(addr, chain)
    cls = _classify_from_sigs(
        timestamps=timestamps,
        fresh_max_txs=fresh_max_txs,
        fresh_max_age_days=fresh_max_age,
        dormant_min_inactive_days=dormant_min,
        now=now,
    )

    expires = now + timedelta(hours=cache_ttl)
    if cached is None:
        # Composite PK is single column (wallet_address) — for cross-
        # chain wallets we'd collide. Distinguish by storing chain;
        # if a chain conflict happens (same string used on both),
        # we just overwrite.
        cached = WalletClassification(wallet_address=cache_key)
        db.add(cached)
    cached.chain = chain
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
                        concurrency: int = 10,
                        chain: str = "solana") -> dict[str, dict]:
    """Batch classifier, chain-aware. Returns {addr: classification}."""
    sem = asyncio.Semaphore(concurrency)

    async def _one(addr: str):
        async with sem:
            return addr, await classify_wallet(addr, db, chain=chain)

    pairs = await asyncio.gather(*[_one(a) for a in addrs],
                                 return_exceptions=True)
    out = {}
    for r in pairs:
        if isinstance(r, Exception):
            continue
        addr, cls = r
        out[addr] = cls
    return out
