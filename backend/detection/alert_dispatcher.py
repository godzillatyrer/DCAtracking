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

from backend import settings_cache
from backend.alerts.telegram_bot import (
    fire_cabal_alert, fire_cabal_exit_alert, fire_sniper_solo_alert,
)
from backend.clients import dexscreener
from backend.database import SessionLocal
from backend.models.alert import Alert
from backend.models.alert_outcome import AlertOutcome
from backend.models.solana_known_wallet import SolanaKnownWallet
from backend.models.solana_wallet_activity import SolanaWalletActivity
from backend.models.solana_wallet_stats import SolanaWalletStats

logger = logging.getLogger(__name__)


# DexScreener returns no market data → treat as "so new, pair not
# indexed yet" → let it through. True for most fresh pump.fun coins.
ALLOW_MISSING_MARKET = True

# Per-mint dedup retention
_DEDUP_HOURS = 24


def _cfg(key: str, default):
    return settings_cache.get(key, default)


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

def _gather_candidates(db: Session, since: datetime, min_same_entity: int) -> list[dict]:
    """Convergence candidates. Only clusters with a dominant funder-
    group of size >= min_same_entity survive. Dormant wallets are
    dropped so retired operators don't pad the count."""
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
            SolanaWalletStats.is_dormant,
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
            SolanaWalletStats.is_dormant,
        )
        .all()
    )

    clusters: dict[str, dict] = {}
    for (mint, wallet, first_buy, last_buy, value_usd,
         role, conf, entity_id, entity_size, dormant) in rows:
        if dormant:
            continue
        c = clusters.setdefault(mint, {
            "mint": mint, "wallets": [], "entity_counts": {},
            "first_buy": first_buy, "last_buy": last_buy,
        })
        c["wallets"].append({
            "address": wallet, "role": role,
            "confidence": float(conf) if conf is not None else 0.0,
            "value_usd": float(value_usd or 0),
            "entity_id": entity_id,
            "entity_size": int(entity_size or 1),
            "first_buy": first_buy, "last_buy": last_buy,
        })
        if entity_id is not None:
            c["entity_counts"][entity_id] = c["entity_counts"].get(entity_id, 0) + 1
        if first_buy and (c["first_buy"] is None or first_buy < c["first_buy"]):
            c["first_buy"] = first_buy
        if last_buy and (c["last_buy"] is None or last_buy > c["last_buy"]):
            c["last_buy"] = last_buy

    qualified = []
    for c in clusters.values():
        dominant = max(c["entity_counts"].values(), default=0)
        c["dominant_entity_count"] = dominant
        c["wallet_count"] = len(c["wallets"])
        c["distinct_entities"] = len(c["entity_counts"]) or c["wallet_count"]
        if dominant < min_same_entity:
            continue
        qualified.append(c)

    qualified.sort(key=lambda c: (-c["dominant_entity_count"], -c["wallet_count"]))
    return qualified


# ─── Market-cap gate ─────────────────────────────────────────────────

async def _check_mc(mint: str, ceiling: float) -> tuple[bool, dict | None]:
    mkt = await dexscreener.token_info(mint)
    if not mkt:
        return (ALLOW_MISSING_MARKET, None)
    mc = mkt.get("market_cap_usd") or 0
    if mc == 0:
        return (ALLOW_MISSING_MARKET, mkt)
    return (mc < ceiling, mkt)


# ─── Sniper-solo candidates ───────────────────────────────────────────

def _gather_sniper_candidates(db: Session, since: datetime) -> list[dict]:
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
            SolanaWalletStats.is_dormant.is_(False),
        )
        .order_by(SolanaWalletActivity.detected_at.desc())
        .all()
    )
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
            "wallet": addr, "mint": mint,
            "value_usd": float(val or 0), "detected_at": detected,
            "role": role,
            "confidence": float(conf or 0),
            "avg_buy_size_usd": float(avg_buy or 0),
            "avg_exit_multiplier": float(avg_mult or 0),
            "best_mint_profit_usd": float(best_profit or 0),
            "win_count": wins or 0, "loss_count": losses or 0,
            "win_rate": (wins / closed) if closed else 0.0,
            "net_profit_usd": float(net_profit or 0),
            "mints_closed": mints_closed or 0,
        })
    out.sort(key=lambda e: (-e["confidence"], -e["avg_exit_multiplier"]))
    return out


# ─── Exit-alert candidates ────────────────────────────────────────────

def _gather_exit_candidates(db: Session, since: datetime, min_same_entity: int) -> list[dict]:
    """Mints being SOLD by N+ wallets from the same entity in the
    window. We only care about mints that an alert had previously
    been fired on — other sells are noise."""
    # Only consider mints we've previously alerted on
    alerted_mints = {
        r[0] for r in db.query(Alert.contract_address).filter(
            Alert.alert_type.in_(("cabal_convergence", "sniper_solo")),
            Alert.fired_at >= datetime.utcnow() - timedelta(days=7),
        ).all()
    }
    if not alerted_mints:
        return []
    rows = (
        db.query(
            SolanaWalletActivity.token_mint,
            SolanaWalletActivity.wallet_address,
            func.max(SolanaWalletActivity.detected_at).label("last_sell"),
            func.sum(SolanaWalletActivity.value_usd).label("value_usd"),
            SolanaWalletStats.entity_id,
        )
        .outerjoin(
            SolanaWalletStats,
            SolanaWalletStats.wallet_address == SolanaWalletActivity.wallet_address,
        )
        .filter(
            SolanaWalletActivity.activity_type == "spl_sell",
            SolanaWalletActivity.detected_at >= since,
            SolanaWalletActivity.token_mint.in_(alerted_mints),
        )
        .group_by(
            SolanaWalletActivity.token_mint,
            SolanaWalletActivity.wallet_address,
            SolanaWalletStats.entity_id,
        )
        .all()
    )
    clusters: dict[str, dict] = {}
    for mint, wallet, last_sell, value_usd, entity_id in rows:
        c = clusters.setdefault(mint, {
            "mint": mint, "wallets": [], "entity_counts": {},
        })
        c["wallets"].append({
            "address": wallet, "last_sell": last_sell,
            "value_usd": float(value_usd or 0), "entity_id": entity_id,
        })
        if entity_id is not None:
            c["entity_counts"][entity_id] = c["entity_counts"].get(entity_id, 0) + 1
    out = []
    for c in clusters.values():
        dominant = max(c["entity_counts"].values(), default=0)
        if dominant < min_same_entity:
            continue
        c["dominant_entity_count"] = dominant
        c["total_value_usd"] = sum(w["value_usd"] for w in c["wallets"])
        out.append(c)
    return out


# ─── Dispatchers ──────────────────────────────────────────────────────

async def _dispatch_convergence(
    db: Session, since: datetime, overrides: dict | None = None,
) -> dict:
    overrides = overrides or {}
    min_same = overrides.get(
        "SAME_ENTITY_MIN_WALLETS", _cfg("SAME_ENTITY_MIN_WALLETS", 3)
    )
    mc_ceiling = overrides.get(
        "FRESH_MAX_MC_USD", _cfg("FRESH_MAX_MC_USD", 50_000.0)
    )
    hour_cap = overrides.get(
        "MAX_ALERTS_PER_HOUR", _cfg("MAX_ALERTS_PER_HOUR", 5)
    )
    is_replay = overrides.get("_replay", False)

    candidates = _gather_candidates(db, since, min_same)
    if is_replay:
        # Just simulate passing the MC gate (using live Dex data)
        passes_count = 0
        for c in candidates:
            p, _ = await _check_mc(c["mint"], mc_ceiling)
            if p:
                passes_count += 1
        return {
            "candidates": len(candidates),
            "would_fire": min(passes_count, hour_cap),
        }

    already = _recently_alerted_mints(db, "cabal_convergence")
    hour_budget = max(
        0, hour_cap - _alerts_sent_last_hour(db, "cabal_convergence")
    )
    if hour_budget == 0:
        return {"fired": 0, "reason": "hourly_cap"}

    fired = 0
    for cluster in candidates:
        if fired >= hour_budget:
            break
        mint = cluster["mint"]
        if mint in already:
            continue
        passes, mkt = await _check_mc(mint, mc_ceiling)
        if not passes:
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
                f"funder cluster bought in the last "
                f"{_cfg('CONVERGENCE_WINDOW_MIN', 60)}m. "
                f"Total buyers: {cluster['wallet_count']}."
            ),
        }
        alert_id = await fire_cabal_alert(event, db=db)
        if alert_id:
            fired += 1
            already.add(mint)
            _create_outcome(db, alert_id, mint, mkt)
    return {"fired": fired}


async def _dispatch_sniper_solo(
    db: Session, since: datetime, overrides: dict | None = None,
) -> dict:
    overrides = overrides or {}
    if not overrides.get("SOLO_ALERTS_ENABLED", _cfg("SOLO_ALERTS_ENABLED", True)):
        return {"fired": 0, "reason": "disabled"}
    mc_ceiling = overrides.get(
        "SOLO_MAX_MC_USD", _cfg("SOLO_MAX_MC_USD", 150_000.0)
    )
    hour_cap = overrides.get(
        "SOLO_MAX_ALERTS_PER_HOUR", _cfg("SOLO_MAX_ALERTS_PER_HOUR", 3)
    )
    is_replay = overrides.get("_replay", False)
    candidates = _gather_sniper_candidates(db, since)

    if is_replay:
        passes_count = 0
        for e in candidates:
            p, _ = await _check_mc(e["mint"], mc_ceiling)
            if p:
                passes_count += 1
        return {
            "candidates": len(candidates),
            "would_fire": min(passes_count, hour_cap),
        }

    already = _recently_alerted_mints(db, "sniper_solo")
    hour_budget = max(0, hour_cap - _alerts_sent_last_hour(db, "sniper_solo"))
    if hour_budget == 0:
        return {"fired": 0, "reason": "hourly_cap"}

    fired = 0
    for event in candidates:
        if fired >= hour_budget:
            break
        mint = event["mint"]
        if mint in already:
            continue
        passes, mkt = await _check_mc(mint, mc_ceiling)
        if not passes:
            continue
        event["market"] = mkt or {}
        event["symbol"] = (mkt or {}).get("symbol")
        event["name"] = (mkt or {}).get("name")
        alert_id = await fire_sniper_solo_alert(event, db=db)
        if alert_id:
            fired += 1
            already.add(mint)
            _create_outcome(db, alert_id, mint, mkt)
    return {"fired": fired}


async def _dispatch_exit_alerts(
    db: Session, overrides: dict | None = None,
) -> dict:
    overrides = overrides or {}
    if not overrides.get("EXIT_ALERTS_ENABLED", _cfg("EXIT_ALERTS_ENABLED", True)):
        return {"fired": 0, "reason": "disabled"}
    min_same = overrides.get("EXIT_MIN_WALLETS", _cfg("EXIT_MIN_WALLETS", 3))
    window_min = overrides.get("EXIT_WINDOW_MIN", _cfg("EXIT_WINDOW_MIN", 10))
    is_replay = overrides.get("_replay", False)
    since = datetime.utcnow() - timedelta(minutes=window_min)

    candidates = _gather_exit_candidates(db, since, min_same)
    if is_replay:
        return {"candidates": len(candidates), "would_fire": len(candidates)}

    already = _recently_alerted_mints(db, "cabal_exit")
    fired = 0
    for c in candidates:
        mint = c["mint"]
        if mint in already:
            continue
        mkt = await dexscreener.token_info(mint)
        event = {
            "mint": mint,
            "symbol": (mkt or {}).get("symbol"),
            "name": (mkt or {}).get("name"),
            "market": mkt or {},
            "wallets": c["wallets"],
            "cabal_count": c["dominant_entity_count"],
            "total_value_usd": c["total_value_usd"],
            "window_min": window_min,
        }
        alert_id = await fire_cabal_exit_alert(event, db=db)
        if alert_id:
            fired += 1
            already.add(mint)
    return {"fired": fired}


def _create_outcome(db: Session, alert_id: int, mint: str, mkt: dict | None) -> None:
    """Record MC/price snapshot at fire time for post-hoc tracking."""
    try:
        existing = db.query(AlertOutcome).filter_by(alert_id=alert_id).first()
        if existing:
            return
        mc = (mkt or {}).get("market_cap_usd") or 0
        price = (mkt or {}).get("price_usd") or 0
        liq = (mkt or {}).get("liquidity_usd") or 0
        db.add(AlertOutcome(
            alert_id=alert_id, mint=mint,
            mc_at_alert=mc if mc else None,
            liq_at_alert=liq if liq else None,
            price_at_alert=price if price else None,
            peak_mc_usd=mc if mc else None,
            peak_at=datetime.utcnow(),
            current_mc_usd=mc if mc else None,
            current_price=price if price else None,
            last_polled_at=datetime.utcnow(),
            fired_at=datetime.utcnow(),
        ))
        db.commit()
    except Exception as e:
        logger.error(f"_create_outcome failed for alert {alert_id}: {e}")
        db.rollback()


async def run_alert_dispatch(overrides: dict | None = None):
    """Called after each activity tracker run. `overrides` is used by
    the replay endpoint — pass `_replay=True` in the dict to simulate
    without persisting alerts/outcomes."""
    db = SessionLocal()
    try:
        window_min = (overrides or {}).get(
            "CONVERGENCE_WINDOW_MIN", _cfg("CONVERGENCE_WINDOW_MIN", 60)
        )
        since = datetime.utcnow() - timedelta(minutes=window_min)
        conv = await _dispatch_convergence(db, since, overrides)
        sniper = await _dispatch_sniper_solo(db, since, overrides)
        exit_ = await _dispatch_exit_alerts(db, overrides)
        return {"convergence": conv, "sniper": sniper, "exit": exit_}
    finally:
        db.close()
