"""
Launch Scorer — the unified tiered alert gate.

Every detection module writes to `launch_signals`. Nothing else fires
alerts directly. This module:

  1. Aggregates signals per (chain, contract_address) into a
     LaunchCandidate row with composite_score + alert_tier.
  2. Decides when to fire Telegram alerts.

Tier rules (enforces "no noise"):

  S  (auto-alert)   : ≥2 HIGH signals converge, OR 1 SELF_S signal
                      (e.g. whale_fresh_wallet which is rare+strong)
  A  (alert w/ AI)  : 1 HIGH + 1 MEDIUM
  B  (silent watch) : 1 HIGH, or 1 MEDIUM, or 2 MEDIUM — DB only
  C  (raw log)      : anything else — DB only

Single signals NEVER trigger S-tier except SELF_S types. That is the
explicit "no stupid alerts" rule.
"""

import logging
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import SessionLocal
from backend.models.launch_candidate import LaunchCandidate
from backend.models.launch_signal import (
    LaunchSignal,
    TIER_HIGH,
    TIER_MEDIUM,
    TIER_FILTER,
    TIER_SELF_S,
)

logger = logging.getLogger(__name__)

# Points assigned per signal tier. Composite score is capped at 100.
TIER_POINTS = {
    TIER_HIGH: 35,
    TIER_MEDIUM: 20,
    TIER_FILTER: 10,
    TIER_SELF_S: 100,
}

# De-dup window for alerts. We never re-alert the same contract within
# this window even if more signals arrive.
ALERT_DEDUP_HOURS = 24

# Stale candidate expiry — if no new signals in this window, mark expired.
CANDIDATE_EXPIRE_HOURS = 72


def _norm(addr: str) -> str:
    """Lower-case EVM addresses; leave Solana addresses untouched (case-sensitive)."""
    if addr.startswith("0x") and len(addr) <= 42:
        return addr.lower()
    return addr


def record_signal(
    db: Session,
    *,
    chain: str,
    contract_address: str,
    signal_type: str,
    signal_tier: str,
    confidence: int = 60,
    evidence: dict | None = None,
    description: str = "",
    deployer_address: str | None = None,
    deploy_tx_hash: str | None = None,
    deploy_block: int | None = None,
    deployed_at: datetime | None = None,
    token_symbol: str | None = None,
    token_name: str | None = None,
    initial_lp_usd: float | None = None,
) -> LaunchSignal:
    """
    Record a signal and upsert the parent LaunchCandidate.

    Detection modules call this and nothing else. The rescoring job
    picks it up on its next run.
    """
    addr = _norm(contract_address)
    points = TIER_POINTS.get(signal_tier, 0)

    # Upsert the candidate
    cand = db.query(LaunchCandidate).filter_by(
        chain=chain, contract_address=addr
    ).first()
    if cand is None:
        cand = LaunchCandidate(
            chain=chain,
            contract_address=addr,
            deployer_address=(deployer_address or "").lower() if deployer_address and deployer_address.startswith("0x") else deployer_address,
            deploy_tx_hash=deploy_tx_hash,
            deploy_block=deploy_block,
            deployed_at=deployed_at,
            token_symbol=token_symbol,
            token_name=token_name,
            initial_lp_usd=initial_lp_usd,
            detected_at=datetime.utcnow(),
            last_signal_at=datetime.utcnow(),
            status="watching",
        )
        db.add(cand)
    else:
        cand.last_signal_at = datetime.utcnow()
        # Fill in fields that might now be known
        for field, value in (
            ("deployer_address", deployer_address),
            ("deploy_tx_hash", deploy_tx_hash),
            ("deploy_block", deploy_block),
            ("deployed_at", deployed_at),
            ("token_symbol", token_symbol),
            ("token_name", token_name),
            ("initial_lp_usd", initial_lp_usd),
        ):
            if value is not None and getattr(cand, field) is None:
                setattr(cand, field, value)

    sig = LaunchSignal(
        chain=chain,
        contract_address=addr,
        signal_type=signal_type,
        signal_tier=signal_tier,
        confidence=int(confidence),
        points=points,
        evidence=evidence or {},
        description=description,
    )
    db.add(sig)
    db.commit()
    return sig


def _load_signals(db: Session, chain: str, address: str) -> list[LaunchSignal]:
    return db.query(LaunchSignal).filter(
        LaunchSignal.chain == chain,
        LaunchSignal.contract_address == address,
    ).all()


def _pick_tier(signals: Iterable[LaunchSignal]) -> tuple[str, int]:
    """
    Decide alert tier + composite score from this candidate's signals.
    Returns (tier, composite_score).
    """
    # De-dup by signal_type: a module can fire the same signal multiple
    # times (e.g. re-confirm), but we only count it once for gating.
    dedup = {}
    for s in signals:
        prev = dedup.get(s.signal_type)
        if prev is None or s.confidence > prev.confidence:
            dedup[s.signal_type] = s
    unique = list(dedup.values())

    high = [s for s in unique if s.signal_tier == TIER_HIGH]
    med = [s for s in unique if s.signal_tier == TIER_MEDIUM]
    filt = [s for s in unique if s.signal_tier == TIER_FILTER]
    self_s = [s for s in unique if s.signal_tier == TIER_SELF_S]

    # Tier decision
    if self_s:
        tier = "S"
    elif len(high) >= 2:
        tier = "S"
    elif high and med:
        tier = "A"
    elif high or len(med) >= 2:
        tier = "B"
    elif med or filt:
        tier = "C"
    else:
        tier = "C"

    # Composite score (capped 100)
    raw = sum(s.points for s in unique)
    # Filter signals only add value when they accompany a real signal
    if not (high or med or self_s):
        raw = max(0, raw - sum(s.points for s in filt))
    composite = min(100, raw)

    return tier, composite


async def recompute_candidate(db: Session, cand: LaunchCandidate) -> None:
    """Recompute score/tier for one candidate. Safe to call repeatedly."""
    signals = _load_signals(db, cand.chain, cand.contract_address)
    tier, score = _pick_tier(signals)

    # Build a summary { signal_type: confidence } for the UI + AI briefing
    summary = {}
    for s in signals:
        prev = summary.get(s.signal_type)
        if prev is None or s.confidence > prev:
            summary[s.signal_type] = s.confidence

    cand.composite_score = score
    cand.alert_tier = tier
    cand.signal_summary = summary

    # Check if we should alert
    if not cand.alert_fired:
        should_alert = tier == "S" or tier == "A"
        if should_alert:
            recent = db.query(LaunchCandidate).filter(
                LaunchCandidate.contract_address == cand.contract_address,
                LaunchCandidate.chain == cand.chain,
                LaunchCandidate.alert_fired.is_(True),
                LaunchCandidate.alert_fired_at >= datetime.utcnow() - timedelta(hours=ALERT_DEDUP_HOURS),
            ).first()
            if recent:
                should_alert = False

        if should_alert:
            # Import here to avoid circular import
            from backend.alerts.launch_alert import fire_launch_alert
            try:
                await fire_launch_alert(cand, signals, tier)
                cand.alert_fired = True
                cand.alert_fired_at = datetime.utcnow()
                cand.alert_tier_fired = tier
                cand.status = "alerted"
            except Exception as e:
                logger.error(f"Failed to fire launch alert for {cand.contract_address}: {e}")


async def run_launch_scorer():
    """
    Scheduler entry point — runs frequently (every ~2 min).
    Recomputes any candidate that got new signals since last run, then
    expires stale candidates.
    """
    logger.info("Starting launch scorer run...")
    db = SessionLocal()
    rescored = 0
    expired = 0
    try:
        # Candidates with any signal in the last scoring window
        window_start = datetime.utcnow() - timedelta(
            minutes=settings.LAUNCH_SCORER_INTERVAL_MIN * 3
        )
        active = db.query(LaunchCandidate).filter(
            LaunchCandidate.last_signal_at >= window_start,
            LaunchCandidate.status != "expired",
        ).all()
        for cand in active:
            try:
                await recompute_candidate(db, cand)
                rescored += 1
            except Exception as e:
                logger.error(f"Rescore error for {cand.contract_address}: {e}")
        db.commit()

        # Expire stale candidates
        cutoff = datetime.utcnow() - timedelta(hours=CANDIDATE_EXPIRE_HOURS)
        expired = db.query(LaunchCandidate).filter(
            LaunchCandidate.last_signal_at < cutoff,
            LaunchCandidate.status == "watching",
        ).update({"status": "expired"}, synchronize_session=False)
        db.commit()
        logger.info(f"Launch scorer complete. Rescored {rescored}, expired {expired}.")
    finally:
        db.close()
