"""
Module 13: Treasury / Multisig Outflow.

High-tier signal when a known VC / project treasury / multisig sends
funds to a fresh wallet. This is a narrower, higher-precision variant
of Module 3 — the funders here are specifically labeled as treasuries.

Implementation sits on top of the WalletFundingEdge pipeline: we
re-query edges whose funder_role='treasury' (or 'vc') and recipient is
a fresh address, and emit a TIER_HIGH signal tied to the recipient. If
the recipient later deploys, the whale_fresh_wallet matcher upgrades
the signal to SELF_S automatically.
"""

import logging
from datetime import datetime, timedelta

from backend.config import settings
from backend.database import SessionLocal
from backend.detection.launch_scorer import record_signal
from backend.models.launch_signal import TIER_HIGH
from backend.models.wallet_funding_graph import WalletFundingEdge

logger = logging.getLogger(__name__)


async def run_treasury_outflow():
    """Scheduler entry."""
    logger.info("Starting treasury_outflow run...")
    db = SessionLocal()
    fires = 0
    try:
        cutoff = datetime.utcnow() - timedelta(hours=6)
        edges = db.query(WalletFundingEdge).filter(
            WalletFundingEdge.funder_role.in_(["treasury", "vc"]),
            WalletFundingEdge.funded_at >= cutoff,
            WalletFundingEdge.recipient_deployed.is_(False),
        ).all()
        for e in edges:
            # Use recipient address as the "contract_address" placeholder until
            # they deploy — the launch_scorer will correlate on deploy_matcher run.
            record_signal(
                db,
                chain=e.chain,
                contract_address=f"pending:{e.recipient_address}",
                signal_type="treasury_outflow",
                signal_tier=TIER_HIGH,
                confidence=78,
                evidence={
                    "treasury": e.funder_address,
                    "treasury_label": e.funder_label,
                    "recipient": e.recipient_address,
                    "amount_usd": str(e.amount_usd or 0),
                    "tx": e.tx_hash,
                },
                description=(
                    f"Treasury {e.funder_label or e.funder_address[:10]} sent "
                    f"${e.amount_usd} to fresh wallet {e.recipient_address[:10]}"
                ),
            )
            fires += 1
    finally:
        db.close()
    logger.info(f"treasury_outflow complete. {fires} signals.")
    return {"signals": fires}
