"""
Module 12: Launchpad Allocation to Known Wallets.

Filter-tier signal. Watches well-known BSC launchpads (PinkSale, DxSale,
UniCrypt, GemPad) for allocation events flowing to wallets in our
known_wallets table. Since most launchpad launches are low-quality, we
treat this as filter-only.

Phase 1: naive implementation that compares Transfer events from
launchpad contracts to recipients in known_wallets over a recent block
window. More sophisticated parsing (per-launchpad event schemas) later.
"""

import logging

from backend.bscscan_client import (
    TRANSFER_TOPIC,
    _chunked_get_logs,
    _hex_to_int,
    _pad_address,
    _unpad_address,
)
from backend.database import SessionLocal
from backend.detection.launch_scorer import record_signal
from backend.models.known_wallet import KnownWallet
from backend.models.launch_signal import TIER_FILTER

logger = logging.getLogger(__name__)


# Well-known BSC launchpads. Expand as we learn of more.
LAUNCHPADS = {
    "PinkSale": "0xe78c4367c386878d57cca455c9593ccd55da1e81",
    "DxSale":   "0x4a6a2355cbd59ed0e5d40b2c3ad7cce7a2fdd032",
    "GemPad":   "0xa9b23d7a2dcd00f06156a6f2cb11a3f38fc1ad0d",
    "UniCrypt": "0x0c89c0407775dd89b12918b9c0aa42bf96518820",
}


async def run_launchpad_watcher():
    logger.info("Starting launchpad_watcher run...")
    db = SessionLocal()
    fires = 0
    try:
        known = {
            w.wallet_address.lower(): w
            for w in db.query(KnownWallet).filter(KnownWallet.is_active.is_(True)).all()
        }
        for name, addr in LAUNCHPADS.items():
            try:
                padded = _pad_address(addr)
                logs = await _chunked_get_logs(
                    {"topics": [TRANSFER_TOPIC, padded]},  # FROM = launchpad
                    total_blocks=5_000,
                )
                recipients: dict[str, list[str]] = {}
                for log in logs:
                    topics = log.get("topics") or []
                    if len(topics) < 3:
                        continue
                    to_addr = _unpad_address(topics[2]).lower()
                    if to_addr not in known:
                        continue
                    token = (log.get("address") or "").lower()
                    recipients.setdefault(token, []).append(to_addr)
                for token, known_list in recipients.items():
                    if not known_list:
                        continue
                    record_signal(
                        db,
                        chain="bsc",
                        contract_address=token,
                        signal_type="launchpad_allocation",
                        signal_tier=TIER_FILTER,
                        confidence=50,
                        evidence={
                            "launchpad": name,
                            "known_recipients": known_list,
                            "count": len(known_list),
                        },
                        description=(
                            f"{len(known_list)} known wallets received allocation "
                            f"from {name} launchpad."
                        ),
                    )
                    fires += 1
            except Exception as e:
                logger.error(f"launchpad scan error ({name}): {e}")
    finally:
        db.close()
    logger.info(f"launchpad_watcher complete. {fires} signals emitted.")
    return {"signals": fires}
