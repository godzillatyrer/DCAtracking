"""
Solana Deployer Watcher — Modules 1 / 2 on Solana.

For every NEW Solana SPL token surfaced by the pair watcher in the
recent past, look up the mint authority via Helius and emit a HIGH-
tier signal if that authority is in solana_known_wallets.

Solana deploys are different from EVM: there's no separate "deployer"
account distinct from the mint authority. The mint authority IS the
key controlled by whoever can issue more of this token. This is the
strongest single piece of "who's behind it" data on Solana.

Memory-safe: we only inspect mints we don't already have a deployer
recorded for, and we cap to 20 mints per run.
"""

import gc
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend.clients import helius
from backend.database import SessionLocal
from backend.detection.launch_scorer import record_signal
from backend.models.launch_candidate import LaunchCandidate
from backend.models.launch_signal import TIER_HIGH
from backend.models.solana_known_wallet import SolanaKnownWallet

logger = logging.getLogger(__name__)

MAX_PER_RUN = 20


def _load_known(db: Session) -> dict[str, SolanaKnownWallet]:
    rows = db.query(SolanaKnownWallet).filter(SolanaKnownWallet.is_active.is_(True)).all()
    return {w.wallet_address: w for w in rows}


async def run_solana_deployer_watcher():
    if not helius.configured:
        logger.info("solana_deployer_watcher: HELIUS_API_KEY not set — skipping.")
        return {"skipped": "no_helius_key"}

    logger.info("Starting Solana deployer watcher run...")
    db = SessionLocal()
    matched = 0
    inspected = 0
    try:
        # Look at recently-detected Solana candidates that don't yet
        # have a deployer_address recorded.
        cutoff = datetime.utcnow() - timedelta(hours=24)
        candidates = (
            db.query(LaunchCandidate)
            .filter(
                LaunchCandidate.chain == "solana",
                LaunchCandidate.deployer_address.is_(None),
                LaunchCandidate.detected_at >= cutoff,
                LaunchCandidate.status != "expired",
            )
            .order_by(LaunchCandidate.detected_at.desc())
            .limit(MAX_PER_RUN)
            .all()
        )
        known = _load_known(db)

        for cand in candidates:
            mint = cand.contract_address
            if mint.startswith("pending:"):
                continue
            try:
                authority, supply, decimals = await helius.get_mint_authority_and_supply(mint)
                inspected += 1
                if not authority:
                    # Mint authority disabled — record but don't signal
                    cand.deployer_address = ""  # empty string = "checked, none"
                    db.commit()
                    continue
                cand.deployer_address = authority
                db.commit()

                # Phase B of Module 3 (Solana): cross-reference the mint
                # authority against recent whale-funding edges.
                try:
                    from backend.detection.solana_whale_fresh import (
                        match_mint_authority_to_funding,
                    )
                    await match_mint_authority_to_funding(db, mint, authority)
                except Exception as e:
                    logger.warning(f"solana whale-fresh match error: {e}")

                kw = known.get(authority)
                if kw:
                    if kw.role in ("golden_deployer", "deployer", "operator"):
                        signal_type = "golden_deployer"
                    else:
                        signal_type = "labeled_entity_deploy"
                    record_signal(
                        db,
                        chain="solana",
                        contract_address=mint,
                        signal_type=signal_type,
                        signal_tier=TIER_HIGH,
                        confidence=82,
                        evidence={
                            "mint": mint,
                            "mint_authority": authority,
                            "label": kw.label,
                            "role": kw.role,
                        },
                        description=(
                            f"Solana mint authority matches known operator: "
                            f"{kw.label}"
                        ),
                        deployer_address=authority,
                    )
                    matched += 1
            except Exception as e:
                logger.error(f"solana mint inspect error {mint}: {e}")
            finally:
                gc.collect()

        logger.info(
            f"Solana deployer watcher complete. Inspected {inspected}, matched {matched}."
        )
    finally:
        db.close()
    return {"inspected": inspected, "matched": matched, "chain": "solana"}
