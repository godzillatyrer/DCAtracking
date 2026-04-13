"""
Alert API routes — alert history and outcome tracking.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models.alert import Alert

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


class AlertOutcomeRequest(BaseModel):
    outcome: str  # pumped, fizzled, still_active
    peak_price: float | None = None
    peak_pct_from_alert: float | None = None
    time_to_peak_hours: int | None = None
    exit_price: float | None = None
    exit_pct: float | None = None
    review_notes: str = ""


@router.get("")
def list_alerts(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    alert_type: str | None = None,
    outcome: str | None = None,
    db: Session = Depends(get_db),
):
    """All alerts, paginated."""
    query = db.query(Alert)

    if alert_type:
        query = query.filter(Alert.alert_type == alert_type)
    if outcome:
        query = query.filter(Alert.outcome == outcome)

    total = query.count()
    alerts = query.order_by(Alert.fired_at.desc()).offset(
        (page - 1) * per_page
    ).limit(per_page).all()

    return {
        "items": [
            {
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
                "peak_price": str(a.peak_price) if a.peak_price else None,
                "peak_pct_from_alert": str(a.peak_pct_from_alert) if a.peak_pct_from_alert else None,
                "time_to_peak_hours": a.time_to_peak_hours,
                "reviewed": a.reviewed,
                "review_notes": a.review_notes,
            }
            for a in alerts
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/{alert_id}")
def get_alert(alert_id: int, db: Session = Depends(get_db)):
    """Single alert detail."""
    alert = db.query(Alert).filter_by(id=alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    return {
        "id": alert.id,
        "contract_address": alert.contract_address,
        "token_symbol": alert.token_symbol,
        "score_at_alert": alert.score_at_alert,
        "alert_type": alert.alert_type,
        "trigger_reason": alert.trigger_reason,
        "ai_briefing": alert.ai_briefing,
        "price_at_alert": str(alert.price_at_alert) if alert.price_at_alert else None,
        "market_cap_at_alert": str(alert.market_cap_at_alert) if alert.market_cap_at_alert else None,
        "telegram_sent": alert.telegram_sent,
        "telegram_message_id": alert.telegram_message_id,
        "fired_at": alert.fired_at.isoformat() if alert.fired_at else None,
        "outcome": alert.outcome,
        "peak_price": str(alert.peak_price) if alert.peak_price else None,
        "peak_pct_from_alert": str(alert.peak_pct_from_alert) if alert.peak_pct_from_alert else None,
        "time_to_peak_hours": alert.time_to_peak_hours,
        "exit_price": str(alert.exit_price) if alert.exit_price else None,
        "exit_pct": str(alert.exit_pct) if alert.exit_pct else None,
        "reviewed": alert.reviewed,
        "review_notes": alert.review_notes,
    }


@router.post("/{alert_id}/outcome")
def log_alert_outcome(
    alert_id: int, request: AlertOutcomeRequest, db: Session = Depends(get_db)
):
    """Log alert outcome (pumped/fizzled) for accuracy tracking."""
    alert = db.query(Alert).filter_by(id=alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    alert.outcome = request.outcome
    alert.reviewed = True
    alert.review_notes = request.review_notes

    if request.peak_price is not None:
        alert.peak_price = request.peak_price
    if request.peak_pct_from_alert is not None:
        alert.peak_pct_from_alert = request.peak_pct_from_alert
    if request.time_to_peak_hours is not None:
        alert.time_to_peak_hours = request.time_to_peak_hours
    if request.exit_price is not None:
        alert.exit_price = request.exit_price
    if request.exit_pct is not None:
        alert.exit_pct = request.exit_pct

    db.commit()

    return {"status": "updated", "alert_id": alert_id, "outcome": request.outcome}


@router.get("/accuracy/summary")
def get_accuracy(db: Session = Depends(get_db)):
    """Win rate and performance metrics."""
    total = db.query(func.count(Alert.id)).scalar()
    reviewed = db.query(Alert).filter(Alert.reviewed.is_(True)).all()

    wins = [a for a in reviewed if a.outcome == "pumped"]
    fizzles = [a for a in reviewed if a.outcome == "fizzled"]

    avg_score_wins = 0
    avg_score_fizzles = 0
    if wins:
        scores = [a.score_at_alert for a in wins if a.score_at_alert]
        avg_score_wins = sum(scores) / len(scores) if scores else 0
    if fizzles:
        scores = [a.score_at_alert for a in fizzles if a.score_at_alert]
        avg_score_fizzles = sum(scores) / len(scores) if scores else 0

    return {
        "total_alerts": total,
        "total_reviewed": len(reviewed),
        "wins": len(wins),
        "fizzles": len(fizzles),
        "pending_review": total - len(reviewed),
        "win_rate": round(len(wins) / len(reviewed) * 100, 1) if reviewed else 0,
        "avg_score_at_win": round(avg_score_wins, 1),
        "avg_score_at_fizzle": round(avg_score_fizzles, 1),
    }
