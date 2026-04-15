"""
Module 3 (Solana variant) — Whale → Fresh Wallet → Mint Deploy.

Two-phase detection mirroring the BSC implementation:

Phase A (funding watcher, every ~10 min):
  Walk recent signatures of every solana_known_wallet with role in
  WHALE_ROLES. For each outgoing SOL transfer above WHALE_FUNDING_MIN_USD
  to a FRESH recipient (signature_count <= FRESH_WALLET_MAX_NONCE),
  record a WalletFundingEdge with chain='solana'.

Phase B (deploy matcher):
  Called from solana_deployer_watcher. When a new SPL token's mint
  authority matches a recipient that was funded by a watched whale
  within WHALE_FRESH_DEPLOY_WINDOW_HOURS, fire SELF_S signal.

Memory: 5 whales per run, 30 sigs each, gc.collect after every wallet.
"""

import gc
import logging
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from backend.clients import defillama, helius
from backend.config import settings
from backend.database import SessionLocal
from backend.detection.launch_scorer import record_signal
from backend.models.launch_signal import TIER_SELF_S
from backend.models.solana_known_wallet import SolanaKnownWallet
from backend.models.wallet_funding_graph import WalletFundingEdge

logger = logging.getLogger(__name__)

WHALE_ROLES = {
    "whale", "treasury", "vc", "founder", "operator",
    "golden_deployer", "distributor",
}
MAX_WHALES_PER_RUN = 5
SIGS_PER_WHALE = 30
LAMPORTS_PER_SOL = 1_000_000_000


async def _sol_price_usd() -> float:
    prices = await defillama.current_prices(["coingecko:solana"])
    entry = prices.get("coingecko:solana") or {}
    return float(entry.get("price") or 0.0)


async def _signature_count(address: str) -> int:
    """Cheap freshness check — number of historical signatures (≈ nonce)."""
    sigs = await helius.get_signatures(address, limit=settings.FRESH_WALLET_MAX_NONCE + 1)
    return len(sigs or [])


def _parse_sol_transfer(tx: dict, sender: str) -> tuple[str | None, int]:
    """
    Walk parsed instructions and return (recipient, lamports_transferred)
    if there is a System-Program transfer FROM sender. Returns (None, 0)
    if no SOL transfer from this sender is found.

    Solana txs are bundles. We only care about 'system' program 'transfer'
    instructions whose source equals our tracked sender.
    """
    try:
        message = ((tx.get("transaction") or {}).get("message") or {})
        instructions = message.get("instructions") or []
        for ix in instructions:
            if not isinstance(ix, dict):
                continue
            program = ix.get("program") or ""
            if program != "system":
                continue
            parsed = ix.get("parsed") or {}
            if (parsed.get("type") or "") != "transfer":
                continue
            info = parsed.get("info") or {}
            if (info.get("source") or "") != sender:
                continue
            recipient = info.get("destination")
            lamports = int(info.get("lamports") or 0)
            return recipient, lamports
    except Exception:
        pass
    return None, 0


async def record_funding_edges():
    """Phase A — scan whale outgoing SOL transfers, record funding edges."""
    if not helius.configured:
        logger.info("solana_whale_fresh: HELIUS_API_KEY not set — skipping.")
        return {"skipped": "no_helius_key"}

    logger.info("Starting Solana whale funding scanner...")
    db = SessionLocal()
    try:
        sol_price = await _sol_price_usd()
        whales = (
            db.query(SolanaKnownWallet)
            .filter(
                SolanaKnownWallet.is_active.is_(True),
                SolanaKnownWallet.role.in_(list(WHALE_ROLES)),
            )
            .limit(MAX_WHALES_PER_RUN)
            .all()
        )

        min_usd = settings.WHALE_FUNDING_MIN_USD
        edges_added = 0

        for w in whales:
            try:
                sigs = await helius.get_signatures(w.wallet_address, limit=SIGS_PER_WHALE)
                for sig_meta in sigs or []:
                    sig = sig_meta.get("signature")
                    if not sig:
                        continue
                    existing = db.query(WalletFundingEdge).filter_by(tx_hash=sig).first()
                    if existing:
                        continue
                    tx = await helius.get_transaction(sig)
                    if not tx:
                        continue
                    recipient, lamports = _parse_sol_transfer(tx, w.wallet_address)
                    if not recipient or lamports <= 0:
                        continue
                    sol_amount = Decimal(lamports) / Decimal(LAMPORTS_PER_SOL)
                    usd_amount = float(sol_amount) * sol_price if sol_price else 0.0
                    if usd_amount < min_usd:
                        continue

                    sig_count = await _signature_count(recipient)
                    if sig_count > settings.FRESH_WALLET_MAX_NONCE:
                        continue

                    slot = tx.get("slot") or 0
                    block_time = tx.get("blockTime")
                    funded_dt = (
                        datetime.utcfromtimestamp(block_time)
                        if block_time
                        else datetime.utcnow()
                    )

                    edge = WalletFundingEdge(
                        chain="solana",
                        funder_address=w.wallet_address,
                        recipient_address=recipient,
                        amount_native=sol_amount,
                        amount_usd=Decimal(str(round(usd_amount, 2))),
                        tx_hash=sig,
                        block_number=int(slot),
                        funded_at=funded_dt,
                        recipient_nonce_at_funding=sig_count,
                        funder_label=w.label,
                        funder_role=w.role,
                    )
                    db.add(edge)
                    edges_added += 1
                    logger.warning(
                        f"SOLANA WHALE→FRESH: {w.label} → fresh "
                        f"{recipient[:10]} (${usd_amount:,.0f})"
                    )
            except Exception as e:
                logger.error(f"solana whale scan {w.wallet_address}: {e}")
            finally:
                gc.collect()
        db.commit()
        logger.info(
            f"Solana whale funding scanner complete. {edges_added} new edges, "
            f"{len(whales)} whales scanned."
        )
        return {"edges_added": edges_added, "whales_scanned": len(whales)}
    finally:
        db.close()


async def match_mint_authority_to_funding(
    db: Session, mint: str, mint_authority: str
) -> None:
    """
    Phase B — invoked from solana_deployer_watcher when we discover a new
    mint's authority. If that authority was funded by a watched whale
    recently, fire SELF_S signal.
    """
    if not mint_authority:
        return
    window_start = datetime.utcnow() - timedelta(
        hours=settings.WHALE_FRESH_DEPLOY_WINDOW_HOURS
    )
    edges = db.query(WalletFundingEdge).filter(
        WalletFundingEdge.chain == "solana",
        WalletFundingEdge.recipient_address == mint_authority,
        WalletFundingEdge.funded_at >= window_start,
    ).all()
    if not edges:
        return
    best = max(edges, key=lambda e: (e.amount_usd or Decimal(0)))
    best.recipient_deployed = True
    best.recipient_deploy_contract = mint
    best.recipient_deploy_at = datetime.utcnow()

    record_signal(
        db,
        chain="solana",
        contract_address=mint,
        signal_type="whale_fresh_wallet",
        signal_tier=TIER_SELF_S,
        confidence=95,
        evidence={
            "mint_authority": mint_authority,
            "funder": best.funder_address,
            "funder_label": best.funder_label,
            "funder_role": best.funder_role,
            "funded_usd": str(best.amount_usd),
            "funded_tx": best.tx_hash,
        },
        description=(
            f"Solana fresh wallet {mint_authority[:10]} was funded "
            f"${best.amount_usd} by {best.funder_label or best.funder_address[:8]} "
            f"and now holds mint authority for a new SPL token."
        ),
        deployer_address=mint_authority,
    )


async def run_solana_whale_fresh_watcher():
    """Scheduler entry — Phase A only (Phase B runs from deployer_watcher)."""
    return await record_funding_edges()
