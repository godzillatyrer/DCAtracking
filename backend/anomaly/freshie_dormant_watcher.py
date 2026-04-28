"""Freshie & Dormant SWARM watcher (Solana).

Every cycle:
  1. Pull the top trending + new pools from GeckoTerminal (Solana).
  2. Drop pools that are too old / too high MC for our purpose.
  3. For each remaining mint, fetch its last ~50 transactions via
     Helius and parse out the unique buyers (wallets whose token
     balance increased).
  4. Classify each buyer (cached): is_fresh / is_dormant.
  5. Per token, count fresh and dormant buyers in this window.
  6. If thresholds met → persist a ChainAnomaly + fire Telegram.

Two distinct alert types:
  - solana_freshie_swarm: "N+ fresh wallets just bought {coin}"
  - solana_dormant_swarm: "N+ long-dormant wallets just woke up to buy {coin}"

Same mint can fire BOTH if both thresholds are met. Per-mint dedup
on each type is handled via the Alert table.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy.orm import Session

from backend import settings_cache
from backend.alerts.telegram_bot import (
    fire_dormant_swarm_alert, fire_freshie_swarm_alert,
)
from backend.anomaly.wallet_classifier import classify_many
from backend.clients import dexscreener, helius
from backend.database import SessionLocal
from backend.models.alert import Alert
from backend.models.chain_anomaly import ChainAnomaly
from backend.models.major_coin import MajorCoin

logger = logging.getLogger(__name__)

GECKO_BASE = "https://api.geckoterminal.com/api/v2"

# How many trending pools to inspect per cycle. Each costs:
#   1 Helius getSignaturesForAddress + ~30 wallet classifications
# Keep modest to fit inside the 8-min job timeout.
MAX_POOLS_PER_CYCLE = 30
SIGS_PER_MINT = 60
TX_PARSE_CONCURRENCY = 10
LAMPORTS_PER_SOL = 1_000_000_000


def _cfg(key, default):
    return settings_cache.get(key, default)


# ─── Trending pool ingestion ─────────────────────────────────────────

async def _fetch_pools(endpoint: str) -> list[dict]:
    """endpoint is e.g. /networks/solana/trending_pools or /new_pools."""
    url = f"{GECKO_BASE}{endpoint}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params={"page": 1})
            if resp.status_code == 200:
                return (resp.json() or {}).get("data") or []
    except Exception as e:
        logger.warning(f"GeckoTerminal {endpoint} fetch failed: {e}")
    return []


async def _get_candidate_mints(majors: set[str]) -> list[dict]:
    """Returns list of {mint, symbol, name, mc_usd, liq_usd, price, pool_url}.
    Combines trending and new pools, dedupes, filters majors."""
    trending, new = await asyncio.gather(
        _fetch_pools("/networks/solana/trending_pools"),
        _fetch_pools("/networks/solana/new_pools"),
        return_exceptions=True,
    )
    pools = []
    for batch in (trending, new):
        if isinstance(batch, Exception) or not batch:
            continue
        pools.extend(batch)

    seen_mints: set[str] = set()
    out = []
    for p in pools:
        try:
            attrs = (p.get("attributes") or {})
            relationships = (p.get("relationships") or {})
            base = (relationships.get("base_token") or {}).get("data") or {}
            base_id = base.get("id") or ""
            # base_id format: "solana_<mint>"
            if not base_id.startswith("solana_"):
                continue
            mint = base_id.split("_", 1)[1]
            if not mint or mint in seen_mints:
                continue
            symbol = (attrs.get("name") or "").split(" / ")[0].strip()
            if symbol.upper() in majors:
                continue
            mc = float(attrs.get("market_cap_usd") or 0) or float(attrs.get("fdv_usd") or 0)
            liq = float(attrs.get("reserve_in_usd") or 0)
            price = float(attrs.get("base_token_price_usd") or 0)
            pool_addr = (attrs.get("address") or "")
            seen_mints.add(mint)
            out.append({
                "mint": mint, "symbol": symbol or None,
                "mc_usd": mc, "liq_usd": liq, "price_usd": price,
                "pool_addr": pool_addr,
            })
        except Exception as e:
            logger.debug(f"pool parse skip: {e}")
            continue
    return out[:MAX_POOLS_PER_CYCLE]


# ─── Buyer extraction ────────────────────────────────────────────────

def _parse_buyers_from_tx(tx: dict, mint: str, sol_price_usd: float) -> list[dict]:
    """Return list of {wallet, sol_amount, value_usd, ts} for owners
    whose balance of `mint` increased in this tx. Mirrors the parser
    in wallet_activity_tracker / cabal_extractor."""
    out = []
    try:
        message = (tx.get("transaction") or {}).get("message") or {}
        meta = tx.get("meta") or {}
        account_keys = message.get("accountKeys") or []

        key_index: dict[str, int] = {}
        for i, k in enumerate(account_keys):
            pk = k.get("pubkey") if isinstance(k, dict) else str(k)
            if pk:
                key_index[pk] = i

        pre = meta.get("preTokenBalances") or []
        post = meta.get("postTokenBalances") or []
        pre_by_owner: dict[str, float] = {}
        post_by_owner: dict[str, float] = {}
        for b in pre:
            if b.get("mint") != mint:
                continue
            o = b.get("owner")
            if o:
                pre_by_owner[o] = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        for b in post:
            if b.get("mint") != mint:
                continue
            o = b.get("owner")
            if o:
                post_by_owner[o] = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)

        pre_sol = meta.get("preBalances") or []
        post_sol = meta.get("postBalances") or []
        block_time = tx.get("blockTime")
        ts = datetime.utcfromtimestamp(block_time) if block_time else datetime.utcnow()

        for owner in (set(pre_by_owner) | set(post_by_owner)):
            delta = post_by_owner.get(owner, 0) - pre_by_owner.get(owner, 0)
            if delta <= 1e-9:    # only buys
                continue
            sol_amt = 0.0
            idx = key_index.get(owner)
            if idx is not None and idx < len(pre_sol) and idx < len(post_sol):
                sol_amt = abs(post_sol[idx] - pre_sol[idx]) / LAMPORTS_PER_SOL
            value_usd = sol_amt * sol_price_usd if sol_price_usd else 0.0
            out.append({
                "wallet": owner, "sol_amount": sol_amt,
                "value_usd": value_usd, "ts": ts,
            })
    except Exception:
        pass
    return out


async def _fetch_buyers(mint: str, sol_price_usd: float) -> list[dict]:
    """Pull last N sigs for a mint, parse for buys, return one row per
    (wallet, signature)."""
    if not helius.configured:
        return []
    sigs = await helius.get_signatures(mint, limit=SIGS_PER_MINT)
    if not sigs:
        return []
    sig_ids = [s.get("signature") for s in sigs if s.get("signature")]

    sem = asyncio.Semaphore(TX_PARSE_CONCURRENCY)

    async def _fetch_tx(sig):
        async with sem:
            return sig, await helius.get_transaction(sig)

    results = await asyncio.gather(
        *[_fetch_tx(s) for s in sig_ids], return_exceptions=True
    )
    buyers = []
    for r in results:
        if isinstance(r, Exception):
            continue
        sig, tx = r
        if not tx:
            continue
        for ev in _parse_buyers_from_tx(tx, mint, sol_price_usd):
            ev["signature"] = sig
            buyers.append(ev)
    return buyers


# ─── Dedup + persistence helpers ─────────────────────────────────────

def _recently_alerted(db: Session, alert_type: str, hours: int) -> set[str]:
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    return {
        r[0] for r in db.query(Alert.contract_address)
        .filter(
            Alert.alert_type == alert_type,
            Alert.fired_at >= cutoff,
        ).all()
    }


# ─── Main entry ──────────────────────────────────────────────────────

async def run_freshie_dormant_watcher() -> dict:
    if not helius.configured:
        return {"skipped": "no_helius_key"}
    if not (
        _cfg("FRESHIE_SWARM_ENABLED", True) or
        _cfg("DORMANT_SWARM_ENABLED", True)
    ):
        return {"skipped": "both_disabled"}

    fresh_min = int(_cfg("FRESHIE_SWARM_MIN_FRESHIES", 4))
    fresh_max_mc = float(_cfg("FRESHIE_SWARM_MAX_MC_USD", 500_000))
    dormant_min = int(_cfg("DORMANT_SWARM_MIN_WALLETS", 3))
    dormant_max_mc = float(_cfg("DORMANT_SWARM_MAX_MC_USD", 5_000_000))
    fresh_dedup = int(_cfg("FRESHIE_SWARM_DEDUP_HOURS", 6))
    dormant_dedup = int(_cfg("DORMANT_SWARM_DEDUP_HOURS", 6))

    db = SessionLocal()
    try:
        # SOL price for value_usd computations on swap legs
        sol_price_usd = 0.0
        try:
            from backend.clients import defillama
            prices = await defillama.current_prices(["coingecko:solana"])
            sol_price_usd = float(
                (prices.get("coingecko:solana") or {}).get("price") or 0
            )
        except Exception:
            pass

        majors = {
            (s or "").upper()
            for (s,) in db.query(MajorCoin.symbol)
            .filter(MajorCoin.excluded.is_(True)).all()
        }

        candidates = await _get_candidate_mints(majors)
        if not candidates:
            return {"checked_pools": 0}

        already_fresh = _recently_alerted(db, "freshie_swarm", fresh_dedup)
        already_dormant = _recently_alerted(db, "dormant_swarm", dormant_dedup)

        fresh_fired = 0
        dormant_fired = 0
        anomalies_added = 0

        # Process each candidate (sequentially per token to bound RPC
        # bursts, but parallel within token via _fetch_buyers).
        for cand in candidates:
            try:
                mint = cand["mint"]
                # Skip if already alerted for BOTH types — saves all the work
                fresh_skip = (mint in already_fresh) or (cand["mc_usd"] and cand["mc_usd"] > fresh_max_mc) or not _cfg("FRESHIE_SWARM_ENABLED", True)
                dormant_skip = (mint in already_dormant) or (cand["mc_usd"] and cand["mc_usd"] > dormant_max_mc) or not _cfg("DORMANT_SWARM_ENABLED", True)
                if fresh_skip and dormant_skip:
                    continue

                buyers = await _fetch_buyers(mint, sol_price_usd)
                if not buyers:
                    continue

                # Aggregate per wallet (one entry per wallet, take earliest)
                per_wallet: dict[str, dict] = {}
                for b in buyers:
                    w = b["wallet"]
                    cur = per_wallet.get(w)
                    if cur is None or b["ts"] < cur["ts"]:
                        per_wallet[w] = b

                addrs = list(per_wallet.keys())
                if not addrs:
                    continue

                classifications = await classify_many(addrs, db, concurrency=10)
                fresh_buyers = []
                dormant_buyers = []
                for w, b in per_wallet.items():
                    cls = classifications.get(w) or {}
                    rec = {**b, **cls}
                    if cls.get("is_fresh"):
                        fresh_buyers.append(rec)
                    if cls.get("is_dormant"):
                        dormant_buyers.append(rec)

                # ─ Fire freshie swarm ──────────────────────────────
                if (
                    not fresh_skip
                    and len(fresh_buyers) >= fresh_min
                ):
                    event = {
                        "mint": mint, "symbol": cand.get("symbol"),
                        "mc_usd": cand.get("mc_usd"),
                        "liq_usd": cand.get("liq_usd"),
                        "price_usd": cand.get("price_usd"),
                        "buyers": fresh_buyers,
                        "type": "freshie_swarm",
                    }
                    alert_id = await fire_freshie_swarm_alert(event, db=db)
                    if alert_id:
                        fresh_fired += 1
                        already_fresh.add(mint)
                        anomalies_added += _persist_anomaly(
                            db, "solana", "freshie_swarm", mint, fresh_buyers,
                            cand, alert_id,
                        )

                # ─ Fire dormant swarm ──────────────────────────────
                if (
                    not dormant_skip
                    and len(dormant_buyers) >= dormant_min
                ):
                    event = {
                        "mint": mint, "symbol": cand.get("symbol"),
                        "mc_usd": cand.get("mc_usd"),
                        "liq_usd": cand.get("liq_usd"),
                        "price_usd": cand.get("price_usd"),
                        "buyers": dormant_buyers,
                        "type": "dormant_swarm",
                    }
                    alert_id = await fire_dormant_swarm_alert(event, db=db)
                    if alert_id:
                        dormant_fired += 1
                        already_dormant.add(mint)
                        anomalies_added += _persist_anomaly(
                            db, "solana", "dormant_swarm", mint, dormant_buyers,
                            cand, alert_id,
                        )
            except Exception as e:
                logger.error(f"freshie_dormant_watcher error on {cand.get('mint','?')[:10]}: {e}")
                db.rollback()

        return {
            "checked_pools": len(candidates),
            "freshie_alerts": fresh_fired,
            "dormant_alerts": dormant_fired,
            "anomalies_added": anomalies_added,
        }
    finally:
        db.close()


def _persist_anomaly(
    db: Session, source: str, event_type: str, mint: str,
    buyers: list[dict], cand: dict, alert_id: int | None,
) -> int:
    """Write one ChainAnomaly per (mint, event_type). Idempotent via
    the unique (source, tx_hash) index — we synthesize a hash from
    (event_type, mint, hour) so the same swarm doesn't double-row in
    a single hour."""
    bucket = datetime.utcnow().strftime("%Y%m%d%H")
    synth_hash = f"{event_type}:{mint}:{bucket}"
    try:
        existing = db.query(ChainAnomaly).filter_by(
            source=source, tx_hash=synth_hash,
        ).first()
        if existing:
            return 0
        notional = sum(float(b.get("value_usd") or 0) for b in buyers)
        actor = buyers[0].get("wallet") if buyers else None
        anom = ChainAnomaly(
            source=source, event_type=event_type,
            coin=cand.get("symbol") or mint[:8],
            side="buy",
            notional_usd=Decimal(str(round(notional, 2))) if notional else None,
            actor_address=actor,
            is_fresh_wallet=(event_type == "freshie_swarm"),
            actor_history_count=len(buyers),
            tx_hash=synth_hash,
            extra_json=json.dumps({
                "mint": mint,
                "buyer_count": len(buyers),
                "mc_usd": cand.get("mc_usd"),
                "liq_usd": cand.get("liq_usd"),
            }),
            is_alerted=alert_id is not None,
            alert_id=alert_id,
        )
        db.add(anom)
        db.commit()
        return 1
    except Exception as e:
        logger.error(f"_persist_anomaly failed: {e}")
        db.rollback()
        return 0
