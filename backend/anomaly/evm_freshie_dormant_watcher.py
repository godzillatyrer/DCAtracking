"""EVM freshie / dormant SWARM watcher (Ethereum + BSC).

Same logic as the Solana version, but EVM:

  1. Pull trending + new pools from GeckoTerminal for the chain.
  2. Filter out major coins (BTC/ETH/SOL/etc. — shared exclusion list).
  3. For each pool, fetch its recent trades from GeckoTerminal
     (`/networks/{chain}/pools/{pool}/trades`).
  4. For each unique buyer (kind=="buy"), classify via Etherscan/
     BscScan: is_fresh / is_dormant.
  5. Per token, count fresh and dormant buyers. If thresholds met
     (and MC under chain ceiling) → ChainAnomaly + Telegram alert.

Two distinct alert types per chain:
  - {chain}_freshie_swarm
  - {chain}_dormant_swarm

The chain (eth/bsc) is recorded in ChainAnomaly.source so the feed
can be filtered.
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
from backend.clients import evm_explorer
from backend.database import SessionLocal
from backend.models.alert import Alert
from backend.models.chain_anomaly import ChainAnomaly
from backend.models.major_coin import MajorCoin

logger = logging.getLogger(__name__)

GECKO_BASE = "https://api.geckoterminal.com/api/v2"

MAX_POOLS_PER_CYCLE = 25      # tighter than Solana since per-pool /trades
                              # call is heavier on GT
TRADES_PER_POOL = 100
TRADE_FETCH_CONCURRENCY = 6


# GeckoTerminal network slugs
NETWORK_BY_CHAIN = {
    "eth": "eth",
    "bsc": "bsc",
}


def _cfg(key, default):
    return settings_cache.get(key, default)


# ─── Trending/new pools per chain ────────────────────────────────────

async def _fetch_pools(network: str, endpoint: str) -> list[dict]:
    url = f"{GECKO_BASE}/networks/{network}{endpoint}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params={"page": 1})
            if resp.status_code == 200:
                return (resp.json() or {}).get("data") or []
    except Exception as e:
        logger.warning(f"GT {url} failed: {e}")
    return []


async def _get_candidates(network: str, majors: set[str]) -> list[dict]:
    trending, new = await asyncio.gather(
        _fetch_pools(network, "/trending_pools"),
        _fetch_pools(network, "/new_pools"),
        return_exceptions=True,
    )
    pools: list[dict] = []
    for batch in (trending, new):
        if isinstance(batch, Exception) or not batch:
            continue
        pools.extend(batch)

    seen_pools: set[str] = set()
    seen_tokens: set[str] = set()
    out: list[dict] = []
    for p in pools:
        try:
            attrs = (p.get("attributes") or {})
            relationships = (p.get("relationships") or {})
            base = (relationships.get("base_token") or {}).get("data") or {}
            base_id = base.get("id") or ""
            # base_id = "<network>_<address>"
            prefix = f"{network}_"
            if not base_id.startswith(prefix):
                continue
            token_addr = base_id[len(prefix):]
            if not token_addr or token_addr.lower() in seen_tokens:
                continue
            symbol_name = (attrs.get("name") or "")
            # GT returns name like "PEPE / WETH" — base symbol is the LHS
            symbol = symbol_name.split(" / ")[0].strip()
            if symbol.upper() in majors:
                continue
            mc = float(attrs.get("market_cap_usd") or 0) or float(attrs.get("fdv_usd") or 0)
            liq = float(attrs.get("reserve_in_usd") or 0)
            price = float(attrs.get("base_token_price_usd") or 0)
            pool_addr = (attrs.get("address") or "")
            if not pool_addr or pool_addr in seen_pools:
                continue
            seen_pools.add(pool_addr)
            seen_tokens.add(token_addr.lower())
            out.append({
                "token_addr": token_addr,
                "pool_addr": pool_addr,
                "symbol": symbol or None,
                "mc_usd": mc, "liq_usd": liq, "price_usd": price,
            })
        except Exception:
            continue
    return out[:MAX_POOLS_PER_CYCLE]


# ─── Pool trades ─────────────────────────────────────────────────────

async def _fetch_pool_trades(network: str, pool_addr: str) -> list[dict]:
    url = f"{GECKO_BASE}/networks/{network}/pools/{pool_addr}/trades"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                return (resp.json() or {}).get("data") or []
            if resp.status_code == 429:
                logger.warning(f"GT trades rate-limited for {pool_addr}")
    except Exception as e:
        logger.debug(f"GT trades fetch failed for {pool_addr}: {e}")
    return []


def _extract_buyers(trades: list[dict], min_buy_usd: float) -> list[dict]:
    """Filter trades for kind=='buy' AND volume_in_usd >= min_buy_usd.
    Returns one row per (wallet, signature). Note: GT's tx_from_address
    is the wallet that initiated the swap = our buyer."""
    out = []
    for t in trades[:TRADES_PER_POOL]:
        try:
            attrs = t.get("attributes") or {}
            kind = attrs.get("kind")  # "buy" or "sell" relative to base token
            if kind != "buy":
                continue
            value_usd = float(attrs.get("volume_in_usd") or 0)
            if value_usd < min_buy_usd:
                continue
            wallet = (attrs.get("tx_from_address") or "").lower()
            if not wallet:
                continue
            ts_str = attrs.get("block_timestamp")
            try:
                ts = datetime.fromisoformat((ts_str or "").replace("Z", "+00:00"))
                # strip tz for consistency with rest of codebase (utc-naive)
                ts = ts.replace(tzinfo=None)
            except Exception:
                ts = datetime.utcnow()
            out.append({
                "wallet": wallet,
                "value_usd": value_usd,
                "ts": ts,
                "tx_hash": attrs.get("tx_hash"),
            })
        except Exception:
            continue
    return out


# ─── Dedup helpers (per-chain alert types) ───────────────────────────

def _alert_type(chain: str, kind: str) -> str:
    return f"{chain}_{kind}_swarm"  # eth_freshie_swarm, bsc_dormant_swarm, ...


def _recently_alerted(db: Session, alert_type: str, hours: int) -> set[str]:
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    return {
        r[0] for r in db.query(Alert.contract_address)
        .filter(
            Alert.alert_type == alert_type,
            Alert.fired_at >= cutoff,
        ).all()
    }


# ─── Persistence ────────────────────────────────────────────────────

def _persist_anomaly(
    db: Session, source: str, event_type: str, mint: str,
    buyers: list[dict], cand: dict, alert_id: int | None,
) -> int:
    """Per (event_type, mint, hour) anomaly row — synthetic dedup key."""
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
            coin=cand.get("symbol") or mint[:10],
            side="buy",
            notional_usd=Decimal(str(round(notional, 2))) if notional else None,
            actor_address=actor,
            is_fresh_wallet=("freshie" in event_type),
            actor_history_count=len(buyers),
            tx_hash=synth_hash,
            extra_json=json.dumps({
                "mint": mint, "buyer_count": len(buyers),
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


# ─── Main ────────────────────────────────────────────────────────────

async def run_evm_freshie_dormant_watcher(chain: str) -> dict:
    """chain: 'eth' or 'bsc'."""
    if chain not in NETWORK_BY_CHAIN:
        return {"error": f"unsupported chain: {chain}"}

    enabled_key = f"{chain.upper()}_FRESHIE_DORMANT_ENABLED"
    if not _cfg(enabled_key, False):
        return {"skipped": "disabled"}

    if not evm_explorer.configured_for(chain):
        return {"skipped": f"no_{chain}_api_key"}

    fresh_min = int(_cfg("FRESHIE_SWARM_MIN_FRESHIES", 4))
    dormant_min = int(_cfg("DORMANT_SWARM_MIN_WALLETS", 3))

    fresh_max_mc = float(_cfg(f"{chain.upper()}_FRESHIE_SWARM_MAX_MC_USD",
                              500_000_000.0 if chain == "bsc" else 100_000_000.0))
    dormant_max_mc = float(_cfg(f"{chain.upper()}_DORMANT_SWARM_MAX_MC_USD",
                                500_000_000.0 if chain == "bsc" else 100_000_000.0))
    min_buy_usd = float(_cfg(f"{chain.upper()}_MIN_BUY_USD",
                             50.0 if chain == "bsc" else 200.0))
    fresh_dedup = int(_cfg("FRESHIE_SWARM_DEDUP_HOURS", 6))
    dormant_dedup = int(_cfg("DORMANT_SWARM_DEDUP_HOURS", 6))

    network = NETWORK_BY_CHAIN[chain]

    db = SessionLocal()
    fresh_fired = 0
    dormant_fired = 0
    anomalies_added = 0
    try:
        majors = {
            (s or "").upper()
            for (s,) in db.query(MajorCoin.symbol)
            .filter(MajorCoin.excluded.is_(True)).all()
        }
        candidates = await _get_candidates(network, majors)
        if not candidates:
            return {"checked_pools": 0}

        fresh_type = _alert_type(chain, "freshie")
        dormant_type = _alert_type(chain, "dormant")
        already_fresh = _recently_alerted(db, fresh_type, fresh_dedup)
        already_dormant = _recently_alerted(db, dormant_type, dormant_dedup)

        # Fan out trade fetches across pools
        sem = asyncio.Semaphore(TRADE_FETCH_CONCURRENCY)

        async def _bounded_trades(cand):
            async with sem:
                trades = await _fetch_pool_trades(network, cand["pool_addr"])
                return cand, trades

        results = await asyncio.gather(
            *[_bounded_trades(c) for c in candidates],
            return_exceptions=True,
        )

        for r in results:
            if isinstance(r, Exception):
                continue
            cand, trades = r
            try:
                token_addr = cand["token_addr"]
                fresh_skip = (
                    token_addr in already_fresh
                    or (cand["mc_usd"] and cand["mc_usd"] > fresh_max_mc)
                )
                dormant_skip = (
                    token_addr in already_dormant
                    or (cand["mc_usd"] and cand["mc_usd"] > dormant_max_mc)
                )
                if fresh_skip and dormant_skip:
                    continue

                buyers_raw = _extract_buyers(trades, min_buy_usd)
                if not buyers_raw:
                    continue

                # Aggregate per wallet (earliest buy in window)
                per_wallet: dict[str, dict] = {}
                for b in buyers_raw:
                    w = b["wallet"]
                    cur = per_wallet.get(w)
                    if cur is None or b["ts"] < cur["ts"]:
                        per_wallet[w] = b

                addrs = list(per_wallet.keys())
                if not addrs:
                    continue

                classifications = await classify_many(
                    addrs, db, concurrency=8, chain=chain,
                )
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
                if not fresh_skip and len(fresh_buyers) >= fresh_min:
                    event = {
                        "mint": token_addr, "symbol": cand.get("symbol"),
                        "mc_usd": cand.get("mc_usd"),
                        "liq_usd": cand.get("liq_usd"),
                        "price_usd": cand.get("price_usd"),
                        "buyers": fresh_buyers,
                        "type": "freshie_swarm",
                        "chain": chain,
                    }
                    alert_id = await fire_freshie_swarm_alert(
                        event, db=db, alert_type=fresh_type,
                    )
                    if alert_id:
                        fresh_fired += 1
                        already_fresh.add(token_addr)
                        anomalies_added += _persist_anomaly(
                            db, chain, "freshie_swarm", token_addr,
                            fresh_buyers, cand, alert_id,
                        )

                # ─ Fire dormant swarm ──────────────────────────────
                if not dormant_skip and len(dormant_buyers) >= dormant_min:
                    event = {
                        "mint": token_addr, "symbol": cand.get("symbol"),
                        "mc_usd": cand.get("mc_usd"),
                        "liq_usd": cand.get("liq_usd"),
                        "price_usd": cand.get("price_usd"),
                        "buyers": dormant_buyers,
                        "type": "dormant_swarm",
                        "chain": chain,
                    }
                    alert_id = await fire_dormant_swarm_alert(
                        event, db=db, alert_type=dormant_type,
                    )
                    if alert_id:
                        dormant_fired += 1
                        already_dormant.add(token_addr)
                        anomalies_added += _persist_anomaly(
                            db, chain, "dormant_swarm", token_addr,
                            dormant_buyers, cand, alert_id,
                        )
            except Exception as e:
                logger.error(f"evm watcher [{chain}] error on {cand.get('token_addr','?')[:10]}: {e}")
                db.rollback()

        return {
            "chain": chain,
            "checked_pools": len(candidates),
            "freshie_alerts": fresh_fired,
            "dormant_alerts": dormant_fired,
            "anomalies_added": anomalies_added,
        }
    finally:
        db.close()


async def run_eth_freshie_dormant_watcher() -> dict:
    return await run_evm_freshie_dormant_watcher("eth")


async def run_bsc_freshie_dormant_watcher() -> dict:
    return await run_evm_freshie_dormant_watcher("bsc")
