"""
Module 6: Same-Operator Different Deployer
Module 11: Burner Sweeping

Both of these walk the `wallet_funding_graph` (+ `wallet_activity`) to
detect operators rotating deployer wallets between launches, or
consolidating tokens from cluster wallets into a single fresh wallet
in preparation for a new launch.

Integration point: runs after deployer_watcher, flags matches against
LaunchCandidates seen in the last WHALE_FRESH_DEPLOY_WINDOW_HOURS.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import SessionLocal
from backend.detection.launch_scorer import record_signal
from backend.models.known_wallet import KnownWallet
from backend.models.launch_candidate import LaunchCandidate
from backend.models.launch_signal import TIER_HIGH, TIER_MEDIUM
from backend.models.wallet_activity import WalletActivity
from backend.models.wallet_funding_graph import WalletFundingEdge

logger = logging.getLogger(__name__)


async def run_operator_graph():
    """Scheduler entry. Emits same-operator + burner-sweeping signals."""
    logger.info("Starting operator_graph run...")
    db = SessionLocal()
    matches = 0
    try:
        window_start = datetime.utcnow() - timedelta(
            hours=settings.WHALE_FRESH_DEPLOY_WINDOW_HOURS
        )
        recent = db.query(LaunchCandidate).filter(
            LaunchCandidate.last_signal_at >= window_start,
            LaunchCandidate.deployer_address.isnot(None),
            LaunchCandidate.status != "expired",
        ).all()

        # Known operators — we match new deployers against this set of funders
        operator_addrs = {
            w.wallet_address.lower()
            for w in db.query(KnownWallet).filter(
                KnownWallet.is_active.is_(True),
                KnownWallet.role.in_(["distributor", "operator", "deployer"]),
            ).all()
        }

        for cand in recent:
            deployer = (cand.deployer_address or "").lower()
            if not deployer:
                continue
            # Same-operator: find any incoming funding edge from a known operator
            edges = db.query(WalletFundingEdge).filter(
                WalletFundingEdge.chain == cand.chain,
                WalletFundingEdge.recipient_address == deployer,
            ).all()
            for e in edges:
                if (e.funder_address or "").lower() in operator_addrs:
                    record_signal(
                        db,
                        chain=cand.chain,
                        contract_address=cand.contract_address,
                        signal_type="same_operator_different_deployer",
                        signal_tier=TIER_HIGH,
                        confidence=80,
                        evidence={
                            "new_deployer": deployer,
                            "known_operator": e.funder_address,
                            "funded_usd": str(e.amount_usd or 0),
                        },
                        description=(
                            "New deployer funded by a known operator "
                            f"({e.funder_label or e.funder_address[:8]})."
                        ),
                    )
                    matches += 1
                    break  # one signal per candidate is enough

        # Burner sweeping (heuristic): known cluster members consolidating
        # tokens into a single fresh wallet in the last N hours
        cutoff = datetime.utcnow() - timedelta(hours=24)
        recent_activity = db.query(WalletActivity).filter(
            WalletActivity.activity_type == "transfer_out",
            WalletActivity.detected_at >= cutoff,
            WalletActivity.counterparty_label == "Known wallet",
        ).all()
        recipient_counts: dict[str, int] = {}
        for a in recent_activity:
            cp = (a.counterparty or "").lower()
            if cp:
                recipient_counts[cp] = recipient_counts.get(cp, 0) + 1
        for addr, n in recipient_counts.items():
            if n < 3:
                continue
            # We don't have a contract address yet — this is pre-launch
            # signal. We synthesize a contract_address = addr so if that
            # address later deploys, its signal will already exist.
            record_signal(
                db,
                chain="bsc",
                contract_address=f"pending:{addr}",
                signal_type="burner_sweeping",
                signal_tier=TIER_MEDIUM,
                confidence=60,
                evidence={"recipient": addr, "incoming_from_cluster": n},
                description=(
                    f"{n} cluster wallets transferred to {addr[:10]} in 24h — "
                    "possible launch prep"
                ),
            )
            matches += 1
    finally:
        db.close()
    logger.info(f"operator_graph complete. {matches} signals emitted.")
    return {"signals": matches}
