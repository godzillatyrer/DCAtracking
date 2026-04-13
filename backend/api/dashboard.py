"""
Dashboard API routes — summary stats and main scanner views.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, case
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models.flagged_token import FlaggedToken
from backend.models.watchlist import Watchlist
from backend.models.alert import Alert
from backend.models.known_wallet import KnownWallet

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/overview")
def get_overview(db: Session = Depends(get_db)):
    """Summary stats: active flags, watchlist count, alert count, win rate."""
    active_flags = db.query(func.count(FlaggedToken.id)).filter(
        FlaggedToken.status.in_(["raw", "candidate"]),
        FlaggedToken.removed.is_(False),
    ).scalar()

    watchlist_count = db.query(func.count(Watchlist.id)).scalar()

    alerts_total = db.query(func.count(Alert.id)).scalar()
    alerts_today = db.query(func.count(Alert.id)).filter(
        func.date(Alert.fired_at) == func.current_date()
    ).scalar()

    # Win rate calculation
    reviewed_alerts = db.query(Alert).filter(Alert.reviewed.is_(True)).all()
    wins = sum(1 for a in reviewed_alerts if a.outcome == "pumped")
    win_rate = (wins / len(reviewed_alerts) * 100) if reviewed_alerts else 0

    known_wallets = db.query(func.count(KnownWallet.id)).filter(
        KnownWallet.is_active.is_(True)
    ).scalar()

    return {
        "active_flags": active_flags,
        "watchlist_count": watchlist_count,
        "alerts_total": alerts_total,
        "alerts_today": alerts_today,
        "win_rate": round(win_rate, 1),
        "known_wallets": known_wallets,
    }


@router.get("/flagged")
def get_flagged_tokens(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    status: str | None = None,
    sort_by: str = "last_seen_at",
    sort_dir: str = "desc",
    db: Session = Depends(get_db),
):
    """All flagged tokens with scores, paginated and sortable."""
    query = db.query(FlaggedToken).filter(FlaggedToken.removed.is_(False))

    if status:
        query = query.filter(FlaggedToken.status == status)

    # Sort
    sort_col = getattr(FlaggedToken, sort_by, FlaggedToken.last_seen_at)
    query = query.order_by(sort_col.desc() if sort_dir == "desc" else sort_col.asc())

    total = query.count()
    tokens = query.offset((page - 1) * per_page).limit(per_page).all()

    # Enrich with watchlist scores
    results = []
    for t in tokens:
        watchlist = db.query(Watchlist).filter_by(contract_address=t.contract_address).first()
        results.append({
            "contract_address": t.contract_address,
            "token_name": t.token_name,
            "token_symbol": t.token_symbol,
            "price_usd": str(t.price_usd) if t.price_usd else None,
            "volume_24h": str(t.volume_24h) if t.volume_24h else None,
            "volume_change_pct": str(t.volume_change_pct) if t.volume_change_pct else None,
            "market_cap": str(t.market_cap) if t.market_cap else None,
            "liquidity_usd": str(t.liquidity_usd) if t.liquidity_usd else None,
            "status": t.status,
            "first_flagged_at": t.first_flagged_at.isoformat() if t.first_flagged_at else None,
            "last_seen_at": t.last_seen_at.isoformat() if t.last_seen_at else None,
            "score": watchlist.current_score if watchlist else 0,
            "confidence": watchlist.confidence_level if watchlist else None,
            "dex_url": t.dex_url,
        })

    return {
        "items": results,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
    }


@router.get("/watchlist")
def get_watchlist(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Watchlist tokens with full details."""
    query = db.query(Watchlist).order_by(Watchlist.current_score.desc())
    total = query.count()
    entries = query.offset((page - 1) * per_page).limit(per_page).all()

    results = []
    for w in entries:
        flagged = db.query(FlaggedToken).filter_by(contract_address=w.contract_address).first()
        results.append({
            "contract_address": w.contract_address,
            "token_name": flagged.token_name if flagged else None,
            "token_symbol": flagged.token_symbol if flagged else None,
            "price_usd": str(flagged.price_usd) if flagged and flagged.price_usd else None,
            "volume_24h": str(flagged.volume_24h) if flagged and flagged.volume_24h else None,
            "market_cap": str(flagged.market_cap) if flagged and flagged.market_cap else None,
            "current_score": w.current_score,
            "score_breakdown": w.score_breakdown,
            "confidence_level": w.confidence_level,
            "cluster_detected": w.cluster_detected,
            "cluster_wallet_count": w.cluster_wallet_count,
            "exchange_deposits_detected": w.exchange_deposits_detected,
            "social_signal_detected": w.social_signal_detected,
            "alert_fired": w.alert_fired,
            "alert_fired_at": w.alert_fired_at.isoformat() if w.alert_fired_at else None,
            "outcome": w.outcome,
            "added_at": w.added_at.isoformat() if w.added_at else None,
            "dex_url": flagged.dex_url if flagged else None,
        })

    return {
        "items": results,
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/alerts")
def get_alerts(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Alert history with outcomes."""
    query = db.query(Alert).order_by(Alert.fired_at.desc())
    total = query.count()
    alerts = query.offset((page - 1) * per_page).limit(per_page).all()

    results = []
    for a in alerts:
        results.append({
            "id": a.id,
            "contract_address": a.contract_address,
            "token_symbol": a.token_symbol,
            "score_at_alert": a.score_at_alert,
            "alert_type": a.alert_type,
            "trigger_reason": a.trigger_reason,
            "ai_briefing": a.ai_briefing,
            "price_at_alert": str(a.price_at_alert) if a.price_at_alert else None,
            "market_cap_at_alert": str(a.market_cap_at_alert) if a.market_cap_at_alert else None,
            "telegram_sent": a.telegram_sent,
            "fired_at": a.fired_at.isoformat() if a.fired_at else None,
            "outcome": a.outcome,
            "peak_pct_from_alert": str(a.peak_pct_from_alert) if a.peak_pct_from_alert else None,
            "reviewed": a.reviewed,
        })

    return {
        "items": results,
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/stats")
def get_stats(db: Session = Depends(get_db)):
    """Win rate, avg lead time, accuracy metrics."""
    reviewed = db.query(Alert).filter(Alert.reviewed.is_(True)).all()

    total_reviewed = len(reviewed)
    wins = [a for a in reviewed if a.outcome == "pumped"]
    fizzles = [a for a in reviewed if a.outcome == "fizzled"]

    avg_peak_gain = 0
    avg_lead_time = 0
    if wins:
        peak_gains = [float(a.peak_pct_from_alert) for a in wins if a.peak_pct_from_alert]
        avg_peak_gain = sum(peak_gains) / len(peak_gains) if peak_gains else 0
        lead_times = [a.time_to_peak_hours for a in wins if a.time_to_peak_hours]
        avg_lead_time = sum(lead_times) / len(lead_times) if lead_times else 0

    return {
        "total_alerts": db.query(func.count(Alert.id)).scalar(),
        "total_reviewed": total_reviewed,
        "wins": len(wins),
        "fizzles": len(fizzles),
        "win_rate": round(len(wins) / total_reviewed * 100, 1) if total_reviewed else 0,
        "avg_peak_gain_pct": round(avg_peak_gain, 1),
        "avg_lead_time_hours": round(avg_lead_time, 1),
    }
