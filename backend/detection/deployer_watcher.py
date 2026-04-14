"""
Module 1: Golden Deployer Watch + Module 2: Labeled Entity Deploy.

Watches for brand-new contract deployments from:
  - Our curated list of "golden deployers" (prior $50M+ launches)
  - Any Arkham-labeled entity (VC, treasury, founder, KOL wallet)

Detection method (BSC):
  We poll `eth_getLogs` for Transfer events with `from = 0x0` (mints)
  over the last N blocks. The recipient of a first mint is typically
  the deployer — we treat it as such for seeding purposes. For brand
  new contracts we cross-check `contractCreator` via our translation
  layer when available.

Solana deployments are Module 2.5 (future) — for Phase 1 we focus BSC.
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from backend.bscscan_client import (
    TRANSFER_TOPIC,
    ZERO_ADDRESS,
    _chunked_get_logs,
    _hex_to_int,
    _rpc_call,
    _unpad_address,
)
from backend.clients import defillama
from backend.config import settings
from backend.database import SessionLocal
from backend.detection.launch_scorer import record_signal
from backend.models.known_wallet import KnownWallet
from backend.models.launch_candidate import LaunchCandidate
from backend.models.launch_signal import TIER_HIGH

logger = logging.getLogger(__name__)

# How many blocks to sweep on each run. BSC is ~3s blocks; at 10-min
# intervals that's ~200 blocks. We sweep 2x to cover scheduler drift.
SWEEP_BLOCKS = 400


async def _recent_contract_deploys() -> list[dict]:
    """
    Return recent contract deployments on BSC.
    Uses Transfer-from-zero events as a proxy (the first mint tx per
    new contract). Each result has: {contract, recipient, block, tx}.
    """
    latest = await _rpc_call("eth_blockNumber", [])
    if not latest:
        return []
    latest_int = _hex_to_int(latest)
    logs = await _chunked_get_logs(
        {"topics": [TRANSFER_TOPIC, ZERO_ADDRESS]},
        total_blocks=SWEEP_BLOCKS,
    )
    # First mint per contract
    first_seen: dict[str, dict] = {}
    for log in logs:
        contract = (log.get("address") or "").lower()
        if not contract or contract in first_seen:
            continue
        topics = log.get("topics") or []
        if len(topics) < 3:
            continue
        recipient = _unpad_address(topics[2])
        block = _hex_to_int(log.get("blockNumber", "0x0"))
        first_seen[contract] = {
            "contract": contract,
            "recipient": recipient.lower(),
            "block": block,
            "tx": log.get("transactionHash") or "",
        }
    # Only return deploys within our sweep window
    cutoff = latest_int - SWEEP_BLOCKS
    return [d for d in first_seen.values() if d["block"] >= cutoff]


def _is_golden_deployer(db: Session, addr: str) -> KnownWallet | None:
    """Match by role=golden_deployer in known_wallets."""
    addr = addr.lower()
    return db.query(KnownWallet).filter(
        KnownWallet.wallet_address == addr,
        KnownWallet.is_active.is_(True),
        KnownWallet.role.in_(["golden_deployer", "deployer", "operator"]),
    ).first()


def _has_entity_label(db: Session, addr: str) -> KnownWallet | None:
    """Any known wallet with a useful label counts for Module 2."""
    addr = addr.lower()
    return db.query(KnownWallet).filter(
        KnownWallet.wallet_address == addr,
        KnownWallet.is_active.is_(True),
    ).first()


async def run_deployer_watcher():
    """
    Scheduler entry point. Every DEPLOYER_WATCHER_INTERVAL_MIN minutes,
    scan recent deploys and emit signals.
    """
    logger.info("Starting deployer watcher run...")
    db = SessionLocal()
    deploys_count = 0
    signal_count = 0
    try:
        deploys = await _recent_contract_deploys()
        logger.info(f"Deployer watcher found {len(deploys)} candidate deploys")
        for d in deploys:
            deploys_count += 1
            # Module 1: Golden Deployer Watch
            golden = _is_golden_deployer(db, d["recipient"])
            if golden:
                record_signal(
                    db,
                    chain="bsc",
                    contract_address=d["contract"],
                    signal_type="golden_deployer",
                    signal_tier=TIER_HIGH,
                    confidence=85,
                    evidence={
                        "deployer": d["recipient"],
                        "deployer_label": golden.label,
                        "associated_token": golden.associated_token,
                        "tx_hash": d["tx"],
                    },
                    description=f"Deployed by golden deployer: {golden.label}",
                    deployer_address=d["recipient"],
                    deploy_tx_hash=d["tx"],
                    deploy_block=d["block"],
                    deployed_at=datetime.utcnow(),
                )
                signal_count += 1
                continue

            # Module 2: Labeled Entity Deploy
            labeled = _has_entity_label(db, d["recipient"])
            if labeled:
                record_signal(
                    db,
                    chain="bsc",
                    contract_address=d["contract"],
                    signal_type="labeled_entity_deploy",
                    signal_tier=TIER_HIGH,
                    confidence=75,
                    evidence={
                        "deployer": d["recipient"],
                        "deployer_label": labeled.label,
                        "role": labeled.role,
                        "tx_hash": d["tx"],
                    },
                    description=f"Deployed by labeled entity: {labeled.label}",
                    deployer_address=d["recipient"],
                    deploy_tx_hash=d["tx"],
                    deploy_block=d["block"],
                    deployed_at=datetime.utcnow(),
                )
                signal_count += 1

        # Also mark the deploy edge in wallet_funding_graph if the deployer
        # appears in a prior funding edge — this is Module 3's trigger.
        try:
            from backend.detection.whale_fresh_wallet import match_deploy_to_funding
            for d in deploys:
                await match_deploy_to_funding(db, d)
        except Exception as e:
            logger.warning(f"Whale-fresh match error: {e}")

    finally:
        db.close()
    logger.info(f"Deployer watcher complete. Deploys={deploys_count} signals={signal_count}")
    return {"deploys": deploys_count, "signals": signal_count}
