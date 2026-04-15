"""Launch candidate API routes."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models.launch_candidate import LaunchCandidate
from backend.models.launch_signal import LaunchSignal

router = APIRouter(prefix="/api/launches", tags=["launches"])


@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    """Summary counts for the Launches dashboard."""
    q = db.query(LaunchCandidate)
    total = q.count()
    alerted = q.filter(LaunchCandidate.alert_fired.is_(True)).count()
    watching = q.filter(LaunchCandidate.status == "watching").count()
    expired = q.filter(LaunchCandidate.status == "expired").count()

    by_tier = {
        tier: q.filter(
            LaunchCandidate.alert_tier == tier,
            LaunchCandidate.status != "expired",
        ).count()
        for tier in ("S", "A", "B", "C")
    }

    return {
        "total": total,
        "alerted": alerted,
        "watching": watching,
        "expired": expired,
        "by_tier": by_tier,
    }


@router.get("/candidates")
def list_candidates(
    tier: str | None = Query(None, description="S | A | B | C"),
    status: str | None = Query(None, description="watching | alerted | expired"),
    chain: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    q = db.query(LaunchCandidate)
    if tier:
        q = q.filter(LaunchCandidate.alert_tier == tier.upper())
    if status:
        q = q.filter(LaunchCandidate.status == status)
    if chain:
        q = q.filter(LaunchCandidate.chain == chain)

    q = q.order_by(
        LaunchCandidate.composite_score.desc(),
        LaunchCandidate.last_signal_at.desc(),
    )
    total = q.count()
    rows = q.offset((page - 1) * per_page).limit(per_page).all()

    items = []
    for c in rows:
        items.append({
            "id": c.id,
            "chain": c.chain,
            "contract_address": c.contract_address,
            "deployer_address": c.deployer_address,
            "deploy_tx_hash": c.deploy_tx_hash,
            "deployed_at": c.deployed_at.isoformat() if c.deployed_at else None,
            "detected_at": c.detected_at.isoformat() if c.detected_at else None,
            "last_signal_at": c.last_signal_at.isoformat() if c.last_signal_at else None,
            "composite_score": c.composite_score,
            "alert_tier": c.alert_tier,
            "signal_summary": c.signal_summary,
            "alert_fired": c.alert_fired,
            "alert_fired_at": c.alert_fired_at.isoformat() if c.alert_fired_at else None,
            "alert_tier_fired": c.alert_tier_fired,
            "token_symbol": c.token_symbol,
            "token_name": c.token_name,
            "initial_lp_usd": str(c.initial_lp_usd) if c.initial_lp_usd else None,
            "status": c.status,
        })

    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.get("/candidate/{candidate_id}/signals")
def candidate_signals(candidate_id: int, db: Session = Depends(get_db)):
    cand = db.query(LaunchCandidate).filter_by(id=candidate_id).first()
    if not cand:
        return {"error": "not found"}
    signals = db.query(LaunchSignal).filter(
        LaunchSignal.chain == cand.chain,
        LaunchSignal.contract_address == cand.contract_address,
    ).order_by(LaunchSignal.detected_at.asc()).all()
    return {
        "candidate": {
            "id": cand.id,
            "chain": cand.chain,
            "contract_address": cand.contract_address,
            "composite_score": cand.composite_score,
            "alert_tier": cand.alert_tier,
        },
        "signals": [
            {
                "id": s.id,
                "signal_type": s.signal_type,
                "signal_tier": s.signal_tier,
                "confidence": s.confidence,
                "points": s.points,
                "description": s.description,
                "evidence": s.evidence,
                "detected_at": s.detected_at.isoformat() if s.detected_at else None,
            }
            for s in signals
        ],
    }
