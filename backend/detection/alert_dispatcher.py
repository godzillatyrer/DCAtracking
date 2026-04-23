"""
Alert Dispatcher — decides when to fire a Telegram notification and
with what payload.

Signal model (cabal convergence, aka "the same team is aping"):
  A new mint is bought within the last WINDOW minutes by N+ tracked
  wallets that share a funder cluster (entity_id). If the mint's
  market cap is under the FRESH_MAX_MC threshold, we alert. Once.

Why this shape:
  - Random "smart money" overlap is noise. Same-cabal-team
    overlap is signal.
  - If MC is already >$50k the play is over. Filter it out.
  - We only want ONE notification per mint regardless of how many
    more wallets pile in later — dedup via the Alert table.

Rate safety:
  - Per-mint 24h dedup (DB)
  - Global hourly cap across ALL mints (anti-storm)

Solo alerts (single top-leaderboard wallet buys NEW mint) are
disabled by default — user explicitly asked for coordinated-buy
signals only.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.alerts.telegram_bot import fire_cabal_alert, fire_sniper_solo_alert
from backend.clients import dexscreener
from backend.database import SessionLocal
from backend.models.alert import Alert
from backend.models.solana_known_wallet import SolanaKnownWallet
from backend.models.solana_wallet_activity import SolanaWalletActivity
from backend.models.solana_wallet_stats import SolanaWalletStats

logger = logging.getLogger(__name__)


# ─── Signal tuning (intentionally strict) ─────────────────────────────

# Window for "buying the same new mint together"
CONVERGENCE_WINDOW_MIN = 60

# The user's rule: "at least 3-4 wallets within the same cabal team"
SAME_ENTITY_MIN_WALLETS = 3

# The user's rule: "only when the token is under $50k MC"
FRESH_MAX_MC_USD = 50_000.0

# If DexScreener returns no market data at all, we assume the mint is
# so new it doesn't have a pair yet (often true for pump.fun coins
# at launch). Let those through — they're exactly the plays we want.
ALLOW_MISSING_MARKET = True

# Per-mint dedup retention
_DEDUP_HOURS = 24

# Global hourly cap for convergence alerts
MAX_ALERTS_PER_HOUR = 5

# Sniper-solo alerts: ONE top-tier sniper (is_sniper=True) buying a
# mint they've never held, under the MC ceiling. Low entries + high
# exit multiples = strong enough signal to fire on one wallet.
SOLO_ALERTS_ENABLED = True
# Separate (lower) cap for sniper alerts — they can be frequent if
# several snipers become active in the same window.
SOLO_MAX_ALERTS_PER_HOUR = 3
# MC ceiling for sniper alerts. Looser than convergence since a good
# sniper often buys right at migration (50-150k). Tune if needed.
SOLO_MAX_MC_USD = 150_000.0


# ─── Helpers ──────────────────────────────────────────────────────────

def _recently_alerted_mints(db: Session, alert_type: str) -> set[str]:
    cutoff = datetime.utcnow() - timedelta(hours=_DEDUP_HOURS)
    return {
        r[0]
        for r in db.query(Alert.contract_address)
        .filter(
            Alert.alert_type == alert_type,
            Alert.fired_at >= cutoff,
        )
        .all()
    }


def _alerts_sent_last_hour(db: Session, alert_type: str) -> int:
    cutoff = datetime.utcnow() - timedelta(hours=1)
    return (
        db.query(func.count(Alert.id))
        .filter(
            Alert.alert_type == alert_type,
            Alert.fired_at >= cutoff,
        )
        .scalar()
        or 0
    )


# ─── Candidate collection ─────────────────────────────────────────────

def _gather_candidates(db: Session, since: datetime) -> list[dict]:
    """One row per (mint, wallet) that bought in the window, enriched
    with role / confidence / entity. Grouped into cluster dicts.
    Only clusters with a dominant funder-group of size
    SAME_ENTITY_MIN_WALLETS survive."""
    rows = (
        db.query(
            SolanaWalletActivity.token_mint,
            SolanaWalletActivity.wallet_address,
            func.min(SolanaWalletActivity.detected_at).label("first_buy"),
            func.max(SolanaWalletActivity.detected_at).label("last_buy"),
            func.sum(SolanaWalletActivity.value_usd).label("value_usd"),
            SolanaKnownWallet.role,
            SolanaWalletStats.confidence_score,
            SolanaWalletStats.entity_id,
            SolanaWalletStats.entity_size,
        )
        .outerjoin(
            SolanaKnownWallet,
            SolanaKnownWallet.wallet_address == SolanaWalletActivity.wallet_address,
        )
        .outerjoin(
            SolanaWalletStats,
            SolanaWalletStats.wallet_address == SolanaWalletActivity.wallet_address,
        )
        .filter(
            SolanaWalletActivity.detected_at >= since,
            SolanaWalletActivity.activity_type == "spl_buy",
            SolanaWalletActivity.token_mint.isnot(None),
        )
        .group_by(
            SolanaWalletActivity.token_mint,
            SolanaWalletActivity.wallet_address,
            SolanaKnownWallet.role,
            SolanaWalletStats.confidence_score,
            SolanaWalletStats.entity_id,
            SolanaWalletStats.entity_size,
        )
        .all()
    )

    # mint -> { wallets: [...], entity_counts: {entity_id: count}, ... }
    clusters: dict[str, dict] = {}
    for (mint, wallet, first_buy, last_buy, value_usd,
         role, conf, entity_id, entity_size) in rows:
        c = clusters.setdefault(mint, {
            "mint": mint,
            "wallets": [],
            "entity_counts": {},
            "first_buy": first_buy,
            "last_buy": last_buy,
        })
        c["wallets"].append({
            "address": wallet,
            "role": role,
            "confidence": float(conf) if conf is not None else 0.0,
            "value_usd": float(value_usd or 0),
            "entity_id": entity_id,
            "entity_size": int(entity_size or 1),
            "first_buy": first_buy,
            "last_buy": last_buy,
        })
        if entity_id is not None:
            c["entity_counts"][entity_id] = c["entity_counts"].get(entity_id, 0) + 1
        if first_buy and (c["first_buy"] is None or first_buy < c["first_buy"]):
            c["first_buy"] = first_buy
        if last_buy and (c["last_buy"] is None or last_buy > c["last_buy"]):
            c["last_buy"] = last_buy

    qualified = []
    for c in clusters.values():
        # Dominant funder cluster size — the biggest group of this
        # mint's buyers that share an entity_id. This is the filter
        # that distinguishes "coordinated cabal aping" from
        # "independent smart money overlap".
        dominant = max(c["entity_counts"].values(), default=0)
        c["dominant_entity_count"] = dominant
        c["wallet_count"] = len(c["wallets"])
        c["distinct_entities"] = len(c["entity_counts"]) or c["wallet_count"]
        if dominant < SAME_ENTITY_MIN_WALLETS:
            continue
        qualified.append(c)

    # Rank by dominant-entity count first (coordination strength),
    # then total wallet count as tiebreaker. Higher = stronger.
    qualified.sort(
        key=lambda c: (-c["dominant_entity_count"], -c["wallet_count"])
    )
    return qualified


# ─── Market-cap gate ─────────────────────────────────────────────────

async def _check_mc(mint: str, ceiling: float) -> tuple[bool, dict | None]:
    """Return (passes_filter, market_info). Missing data → passes."""
    mkt = await dexscreener.token_info(mint)
    if not mkt:
        return (ALLOW_MISSING_MARKET, None)
    mc = mkt.get("market_cap_usd") or 0
    if mc == 0:
        return (ALLOW_MISSING_MARKET, mkt)
    return (mc < ceiling, mkt)


# ─── Sniper-solo candidates ────────────────────────────────────────────

def _gather_sniper_candidates(db: Session, since: datetime) -> list[dict]:
    """Buys by is_sniper wallets on mints they've never held before,
    in the window. One row per (wallet, mint) — we only alert on the
    first buy within the cycle even if the wallet bought multiple
    times in the same tx batch."""
    rows = (
        db.query(
            SolanaWalletActivity.wallet_address,
            SolanaWalletActivity.token_mint,
            SolanaWalletActivity.value_usd,
            SolanaWalletActivity.detected_at,
            SolanaKnownWallet.role,
            SolanaWalletStats.confidence_score,
            SolanaWalletStats.avg_buy_size_usd,
            SolanaWalletStats.avg_exit_multiplier,
            SolanaWalletStats.best_mint_profit_usd,
            SolanaWalletStats.win_count,
            SolanaWalletStats.loss_count,
            SolanaWalletStats.net_profit_usd,
            SolanaWalletStats.mints_closed,
        )
        .outerjoin(
            SolanaKnownWallet,
            SolanaKnownWallet.wallet_address == SolanaWalletActivity.wallet_address,
        )
        .join(
            SolanaWalletStats,
            SolanaWalletStats.wallet_address == SolanaWalletActivity.wallet_address,
        )
        .filter(
            SolanaWalletActivity.activity_type == "spl_buy",
            SolanaWalletActivity.is_new_token.is_(True),
            SolanaWalletActivity.detected_at >= since,
            SolanaWalletStats.is_sniper.is_(True),
        )
        .order_by(SolanaWalletActivity.detected_at.desc())
        .all()
    )
    # Collapse by (wallet, mint) — earliest buy wins
    seen = set()
    out = []
    for (addr, mint, val, detected, role, conf, avg_buy, avg_mult,
         best_profit, wins, losses, net_profit, mints_closed) in rows:
        key = (addr, mint)
        if key in seen:
            continue
        seen.add(key)
        closed = (wins or 0) + (losses or 0)
        out.append({
            "wallet": addr,
            "mint": mint,
            "value_usd": float(val or 0),
            "detected_at": detected,
            "role": role,
            "confidence": float(conf or 0),
            "avg_buy_size_usd": float(avg_buy or 0),
            "avg_exit_multiplier": float(avg_mult or 0),
            "best_mint_profit_usd": float(best_profit or 0),
            "win_count": wins or 0,
            "loss_count": losses or 0,
            "win_rate": (wins / closed) if closed else 0.0,
            "net_profit_usd": float(net_profit or 0),
            "mints_closed": mints_closed or 0,
        })
    # Best signal first: highest confidence, then highest avg exit mult
    out.sort(key=lambda e: (-e["confidence"], -e["avg_exit_multiplier"]))
    return out


# ─── Entry point ──────────────────────────────────────────────────────

async def _dispatch_convergence(db: Session, since: datetime) -> int:
    already = _recently_alerted_mints(db, "cabal_convergence")
    hour_budget = max(0, MAX_ALERTS_PER_HOUR - _alerts_sent_last_hour(db, "cabal_convergence"))
    if hour_budget == 0:
        logger.info(
            f"alert_dispatch: convergence hourly cap reached ({MAX_ALERTS_PER_HOUR})"
        )
        return 0

    fired = 0
    for cluster in _gather_candidates(db, since):
        if fired >= hour_budget:
            break
        mint = cluster["mint"]
        if mint in already:
            continue

        passes, mkt = await _check_mc(mint, FRESH_MAX_MC_USD)
        if not passes:
            logger.info(
                f"alert_dispatch: skipped convergence {mint[:10]} — MC "
                f"{mkt.get('market_cap_usd') if mkt else 'unknown'} "
                f"above {FRESH_MAX_MC_USD:,.0f}"
            )
            continue

        event = {
            "mint": mint,
            "symbol": (mkt or {}).get("symbol"),
            "name": (mkt or {}).get("name"),
            "market": mkt or {},
            "wallets": cluster["wallets"],
            "cabal_count": cluster["dominant_entity_count"],
            "entity_count": cluster["distinct_entities"],
            "trigger_reason": (
                f"{cluster['dominant_entity_count']} wallets from the same "
                f"funder cluster bought in the last {CONVERGENCE_WINDOW_MIN}m. "
                f"Total buyers: {cluster['wallet_count']}."
            ),
        }
        if await fire_cabal_alert(event, db=db):
            fired += 1
            already.add(mint)
    return fired


async def _dispatch_sniper_solo(db: Session, since: datetime) -> int:
    if not SOLO_ALERTS_ENABLED:
        return 0
    already = _recently_alerted_mints(db, "sniper_solo")
    hour_budget = max(
        0, SOLO_MAX_ALERTS_PER_HOUR - _alerts_sent_last_hour(db, "sniper_solo")
    )
    if hour_budget == 0:
        logger.info(
            f"alert_dispatch: sniper hourly cap reached ({SOLO_MAX_ALERTS_PER_HOUR})"
        )
        return 0

    fired = 0
    for event in _gather_sniper_candidates(db, since):
        if fired >= hour_budget:
            break
        mint = event["mint"]
        if mint in already:
            continue
        passes, mkt = await _check_mc(mint, SOLO_MAX_MC_USD)
        if not passes:
            continue
        event["market"] = mkt or {}
        event["symbol"] = (mkt or {}).get("symbol")
        event["name"] = (mkt or {}).get("name")
        if await fire_sniper_solo_alert(event, db=db):
            fired += 1
            already.add(mint)
    return fired


async def run_alert_dispatch():
    """Called after each activity tracker run."""
    db = SessionLocal()
    try:
        since = datetime.utcnow() - timedelta(minutes=CONVERGENCE_WINDOW_MIN)
        convergence_fired = await _dispatch_convergence(db, since)
        sniper_fired = await _dispatch_sniper_solo(db, since)
        if convergence_fired or sniper_fired:
            logger.info(
                f"alert_dispatch: {convergence_fired} convergence + "
                f"{sniper_fired} sniper solo alerts fired"
            )
        return {
            "convergence_fired": convergence_fired,
            "sniper_fired": sniper_fired,
            "solo_enabled": SOLO_ALERTS_ENABLED,
        }
    finally:
        db.close()
