"""CEX-funded accumulation alerter.

Joins the activity log against the cex_funded wallet population. For
every mint the group is net-long on, computes:

  - aggregate net buy USD (sum of buy USD - sum of sell USD)
  - per-wallet net token balance (cum_bought - cum_sold)
  - group's combined % of token supply (via Helius getAccountInfo)
  - sell ratio (group sell USD / group buy USD)
  - hold span (latest buy - earliest buy)

Fires when the group crosses the supply % threshold on a token in the
target MC band, with an active accumulation pattern (not a flip).

Why this catches the USDUC pattern:
  - Binance-funded wallets accumulate $TOKEN over weeks → all of them
    end up in solana_known_wallets with role='cex_funded'
  - Each of their buys lands in solana_wallet_activity
  - When ≥3 of them collectively hold ≥1.5% of supply on a coin in the
    $500k–$20M MC band, and they're not selling, we fire.

Tunable via Settings UI (CEX_ACCUMULATION_*). Defaults are in
settings_cache.DEFAULTS.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from backend import settings_cache
from backend.alerts.telegram_bot import fire_cex_accumulation_alert
from backend.clients import dexscreener, helius
from backend.database import SessionLocal
from backend.models.alert import Alert
from backend.models.major_coin import MajorCoin
from backend.models.solana_known_wallet import SolanaKnownWallet
from backend.models.solana_wallet_activity import SolanaWalletActivity

logger = logging.getLogger(__name__)

# SPL infra mints we never alert on.
_SKIP_MINTS = {
    "So11111111111111111111111111111111111111112",  # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

# Don't keep more than this many candidate mints in memory per cycle.
# Anything past this is probably noise — pump.fun apes touching dozens
# of tokens.
MAX_CANDIDATE_MINTS = 200

# Concurrency for supply lookups (one Helius RPC each).
SUPPLY_LOOKUP_CONCURRENCY = 8


def _cfg(key, default):
    return settings_cache.get(key, default)


# ─── Candidate aggregation ───────────────────────────────────────────

def _aggregate_candidates(
    db: Session, lookback: timedelta, min_wallets: int,
) -> list[dict]:
    """For each mint, build a per-wallet net-position summary across
    cex_funded wallets active in the lookback window.

    Returns: [{mint, wallets:[{address, funding_cex, net_tokens,
                                buy_usd, sell_usd, net_usd, first_buy,
                                last_buy}]}]
    """
    since = datetime.utcnow() - lookback

    rows = (
        db.query(
            SolanaWalletActivity.token_mint,
            SolanaWalletActivity.wallet_address,
            SolanaKnownWallet.funding_source,
            func.sum(
                case(
                    (SolanaWalletActivity.activity_type == "spl_buy",
                     SolanaWalletActivity.amount),
                    else_=Decimal("0"),
                )
            ).label("bought_tokens"),
            func.sum(
                case(
                    (SolanaWalletActivity.activity_type == "spl_sell",
                     SolanaWalletActivity.amount),
                    else_=Decimal("0"),
                )
            ).label("sold_tokens"),
            func.sum(
                case(
                    (SolanaWalletActivity.activity_type == "spl_buy",
                     SolanaWalletActivity.value_usd),
                    else_=Decimal("0"),
                )
            ).label("buy_usd"),
            func.sum(
                case(
                    (SolanaWalletActivity.activity_type == "spl_sell",
                     SolanaWalletActivity.value_usd),
                    else_=Decimal("0"),
                )
            ).label("sell_usd"),
            func.min(SolanaWalletActivity.detected_at).label("first_buy"),
            func.max(SolanaWalletActivity.detected_at).label("last_buy"),
        )
        .join(
            SolanaKnownWallet,
            SolanaKnownWallet.wallet_address == SolanaWalletActivity.wallet_address,
        )
        .filter(
            SolanaKnownWallet.role == "cex_funded",
            SolanaKnownWallet.is_active.is_(True),
            SolanaWalletActivity.detected_at >= since,
            SolanaWalletActivity.token_mint.isnot(None),
            SolanaWalletActivity.activity_type.in_(("spl_buy", "spl_sell")),
        )
        .group_by(
            SolanaWalletActivity.token_mint,
            SolanaWalletActivity.wallet_address,
            SolanaKnownWallet.funding_source,
        )
        .all()
    )

    by_mint: dict[str, list[dict]] = {}
    for r in rows:
        mint = r.token_mint
        if not mint or mint in _SKIP_MINTS:
            continue
        net_tokens = float(r.bought_tokens or 0) - float(r.sold_tokens or 0)
        if net_tokens <= 0:
            continue
        funding_cex = "unknown"
        if r.funding_source and ":" in r.funding_source:
            funding_cex = r.funding_source.split(":", 1)[0]
        elif r.funding_source:
            funding_cex = r.funding_source
        by_mint.setdefault(mint, []).append({
            "address": r.wallet_address,
            "funding_source": r.funding_source,
            "funding_cex": funding_cex,
            "net_tokens": net_tokens,
            "buy_usd": float(r.buy_usd or 0),
            "sell_usd": float(r.sell_usd or 0),
            "net_usd": float(r.buy_usd or 0) - float(r.sell_usd or 0),
            "first_buy": r.first_buy,
            "last_buy": r.last_buy,
        })

    candidates = []
    for mint, wallets in by_mint.items():
        if len(wallets) < min_wallets:
            continue
        candidates.append({"mint": mint, "wallets": wallets})

    # Order by group net_usd so we surface biggest first if we hit caps.
    candidates.sort(
        key=lambda c: -sum(w["net_usd"] for w in c["wallets"]),
    )
    return candidates[:MAX_CANDIDATE_MINTS]


# ─── Supply lookup ───────────────────────────────────────────────────

async def _get_token_supply(
    mint: str,
) -> tuple[float | None, int | None]:
    """Return (supply_in_human_units, decimals) for an SPL mint.
    None on any failure."""
    _auth, raw_supply, decimals = await helius.get_mint_authority_and_supply(mint)
    if raw_supply is None or decimals is None:
        return (None, None)
    try:
        return (raw_supply / (10 ** decimals), decimals)
    except Exception:
        return (None, None)


# ─── Main entry ──────────────────────────────────────────────────────

async def run_accumulation_alerter() -> dict:
    if not helius.configured:
        logger.info("accumulation_alerter: HELIUS_API_KEY not set — skipping")
        return {"skipped": "no_helius_key"}
    if not _cfg("CEX_ACCUMULATION_ENABLED", True):
        return {"skipped": "disabled"}

    min_wallets = int(_cfg("CEX_ACCUMULATION_MIN_WALLETS", 3))
    min_supply_pct = float(_cfg("CEX_ACCUMULATION_MIN_SUPPLY_PCT", 1.5))
    min_net_per_wallet = float(_cfg(
        "CEX_ACCUMULATION_MIN_NET_USD_PER_WALLET", 500.0,
    ))
    min_hold_hours = float(_cfg("CEX_ACCUMULATION_MIN_HOLD_HOURS", 24.0))
    max_sell_ratio = float(_cfg("CEX_ACCUMULATION_MAX_SELL_RATIO", 0.30))
    min_mc = float(_cfg("CEX_ACCUMULATION_MIN_MC_USD", 500_000.0))
    max_mc = float(_cfg("CEX_ACCUMULATION_MAX_MC_USD", 20_000_000.0))
    dedup_hours = int(_cfg("CEX_ACCUMULATION_DEDUP_HOURS", 168))
    hour_cap = int(_cfg("CEX_ACCUMULATION_MAX_ALERTS_PER_HOUR", 4))

    # 30 days of activity is the default lookback. Stealth accumulation
    # often takes 1-3 weeks; longer than 30 days starts including
    # already-pumped exit positions.
    lookback = timedelta(days=30)

    db = SessionLocal()
    try:
        # Major-coin exclusion (SOL, BTC, ETH, USDC, USDT, etc.)
        majors = {
            (s or "").upper()
            for (s,) in db.query(MajorCoin.symbol)
            .filter(MajorCoin.excluded.is_(True)).all()
        }

        # Per-mint dedup
        dedup_cutoff = datetime.utcnow() - timedelta(hours=dedup_hours)
        already = {
            r[0] for r in db.query(Alert.contract_address)
            .filter(
                Alert.alert_type == "cex_accumulation",
                Alert.fired_at >= dedup_cutoff,
            ).all()
        }

        # Hourly cap
        hour_cutoff = datetime.utcnow() - timedelta(hours=1)
        sent_this_hour = (
            db.query(func.count(Alert.id))
            .filter(
                Alert.alert_type == "cex_accumulation",
                Alert.fired_at >= hour_cutoff,
            ).scalar() or 0
        )
        budget = max(0, hour_cap - sent_this_hour)

        candidates = _aggregate_candidates(db, lookback, min_wallets)
        if not candidates:
            return {"candidates": 0}

        # Pre-filter cheap (per-wallet thresholds) before paying for
        # supply + dexscreener lookups.
        cheap_filtered: list[dict] = []
        for cand in candidates:
            mint = cand["mint"]
            if mint in already:
                continue
            qual_wallets = [
                w for w in cand["wallets"]
                if w["net_usd"] >= min_net_per_wallet
            ]
            if len(qual_wallets) < min_wallets:
                continue
            # Must have stealth pattern: earliest buy ≥ N hours ago
            now = datetime.utcnow()
            earliest = min(w["first_buy"] for w in qual_wallets)
            hours_since_earliest = (now - earliest).total_seconds() / 3600
            if hours_since_earliest < min_hold_hours:
                continue
            # Sell ratio gate
            total_buy = sum(w["buy_usd"] for w in qual_wallets) or 0.0
            total_sell = sum(w["sell_usd"] for w in qual_wallets) or 0.0
            sell_ratio = (total_sell / total_buy) if total_buy > 0 else 0.0
            if sell_ratio > max_sell_ratio:
                continue
            cand["wallets"] = qual_wallets
            cand["earliest_buy"] = earliest
            cand["sell_ratio"] = sell_ratio
            cheap_filtered.append(cand)

        if not cheap_filtered:
            return {"candidates": len(candidates), "post_cheap_filter": 0}

        # Resolve supply + market in parallel
        sem = asyncio.Semaphore(SUPPLY_LOOKUP_CONCURRENCY)

        async def _enrich(cand):
            mint = cand["mint"]
            async with sem:
                supply_human, _decimals = await _get_token_supply(mint)
            mkt = await dexscreener.token_info(mint)
            return cand, supply_human, mkt

        enriched = await asyncio.gather(
            *[_enrich(c) for c in cheap_filtered],
            return_exceptions=True,
        )

        fired = 0
        evaluated = 0
        skipped: dict[str, int] = {
            "no_supply": 0, "no_market": 0, "wrong_mc_band": 0,
            "below_supply_pct": 0, "major_coin": 0,
        }

        for r in enriched:
            if budget <= 0:
                break
            if isinstance(r, Exception):
                logger.warning(f"accumulation enrich error: {r}")
                continue
            cand, supply_human, mkt = r
            mint = cand["mint"]
            evaluated += 1

            symbol = (mkt or {}).get("symbol") or ""
            if symbol and symbol.upper() in majors:
                skipped["major_coin"] += 1
                continue

            if not supply_human or supply_human <= 0:
                skipped["no_supply"] += 1
                continue

            mc = (mkt or {}).get("market_cap_usd") or 0
            if not mc:
                skipped["no_market"] += 1
                continue
            if mc < min_mc or mc > max_mc:
                skipped["wrong_mc_band"] += 1
                continue

            # Compute supply %
            total_net = sum(w["net_tokens"] for w in cand["wallets"])
            total_pct = (total_net / supply_human) * 100.0 if supply_human else 0.0
            if total_pct < min_supply_pct:
                skipped["below_supply_pct"] += 1
                continue

            # Per-wallet supply % + days_held
            now = datetime.utcnow()
            for w in cand["wallets"]:
                w["supply_pct"] = (
                    (w["net_tokens"] / supply_human) * 100.0
                    if supply_human else 0.0
                )
                first = w["first_buy"]
                w["days_held"] = (
                    (now - first).total_seconds() / 86400 if first else 0.0
                )

            cex_breakdown: dict[str, int] = {}
            for w in cand["wallets"]:
                cex_breakdown[w["funding_cex"]] = cex_breakdown.get(
                    w["funding_cex"], 0,
                ) + 1

            earliest = cand["earliest_buy"]
            latest = max(w["last_buy"] for w in cand["wallets"])
            days_span = (
                (latest - earliest).total_seconds() / 86400 if earliest and latest else 0.0
            )
            total_net_usd = sum(w["net_usd"] for w in cand["wallets"])

            event = {
                "mint": mint,
                "symbol": (mkt or {}).get("symbol"),
                "name": (mkt or {}).get("name"),
                "market": mkt or {},
                "wallets": cand["wallets"],
                "wallet_count": len(cand["wallets"]),
                "total_supply_pct": total_pct,
                "cex_breakdown": cex_breakdown,
                "days_span": days_span,
                "total_net_usd": total_net_usd,
                "sell_ratio": cand["sell_ratio"],
            }

            try:
                alert_id = await fire_cex_accumulation_alert(event, db=db)
                if alert_id:
                    fired += 1
                    budget -= 1
                    already.add(mint)
            except Exception as e:
                logger.error(f"fire_cex_accumulation_alert failed for {mint[:10]}: {e}")

        return {
            "candidates": len(candidates),
            "post_cheap_filter": len(cheap_filtered),
            "evaluated": evaluated,
            "fired": fired,
            "skipped": skipped,
        }
    finally:
        db.close()
