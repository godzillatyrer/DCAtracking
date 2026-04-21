"""
Solana Graph Walk — follow the money from cabal wallets.

Cabal operators rotate wallets after each play: Wallet A profits →
transfers SOL to Wallet B (fresh) → Wallet B buys the next token.
If we only track Wallet A, we lose them.

This module runs periodically (every 15 min) and:
  1. Picks cabal_trader + cabal_linked wallets from solana_known_wallets
  2. Pulls their recent outgoing SOL transfers via Helius
  3. If recipient is "fresh" (< 5 signatures), auto-adds it as
     role='cabal_linked' (depth 1) or 'cabal_linked_2' (depth 2)
  4. Records the edge in wallet_funding_edges
  5. Stops at depth 2 to avoid graph explosion

This means: if a cabal trader from yesterday rotates into a fresh
wallet today, that fresh wallet is automatically tracked. When it
buys into a new token, Module 5 (insider_early_buyer) fires.

Memory-bounded: MAX_WALLETS_PER_RUN random sample, MAX_SIGS_PER_WALLET,
gc.collect between wallets.
"""

import gc
import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from backend.clients import helius, defillama
from backend.config import settings
from backend.database import SessionLocal
from backend.models.solana_known_wallet import SolanaKnownWallet
from backend.models.wallet_funding_graph import WalletFundingEdge

logger = logging.getLogger(__name__)

CABAL_ROLES = {"cabal_trader", "cabal_linked"}
MAX_WALLETS_PER_RUN = 30
MAX_SIGS_PER_WALLET = 15
FRESH_WALLET_MAX_SIGS = 5
MIN_SOL_TRANSFER = 0.5   # minimum SOL transfer to track (skip gas dust)
LAMPORTS_PER_SOL = 1_000_000_000


async def _sol_price_usd() -> float:
    prices = await defillama.current_prices(["coingecko:solana"])
    entry = prices.get("coingecko:solana") or {}
    return float(entry.get("price") or 0.0)


async def _sig_count(address: str) -> int:
    sigs = await helius.get_signatures(address, limit=FRESH_WALLET_MAX_SIGS + 1)
    return len(sigs or [])


def _parse_sol_transfer(tx: dict, sender: str) -> tuple[str | None, int]:
    """Extract (recipient, lamports) from a System.transfer in a parsed tx."""
    try:
        message = ((tx.get("transaction") or {}).get("message") or {})
        for ix in message.get("instructions") or []:
            if not isinstance(ix, dict):
                continue
            if (ix.get("program") or "") != "system":
                continue
            parsed = ix.get("parsed") or {}
            if (parsed.get("type") or "") != "transfer":
                continue
            info = parsed.get("info") or {}
            if (info.get("source") or "") != sender:
                continue
            return info.get("destination"), int(info.get("lamports") or 0)
    except Exception:
        pass
    return None, 0


def _is_exchange_or_program(addr: str) -> bool:
    """Quick filter for well-known Solana programs / exchanges. These
    are NOT cabal rotations — they're cashing out or interacting with
    protocols."""
    # Known program IDs + common Solana infra
    SKIP = {
        "11111111111111111111111111111111",
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
        "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
    }
    return addr in SKIP


async def _walk_wallet(
    wallet: SolanaKnownWallet,
    db: Session,
    known_addrs: set[str],
    sol_price: float,
    depth_label: str,
) -> int:
    """Walk one wallet's recent outgoing SOL transfers. Returns count of
    new wallets discovered."""
    found = 0
    sigs = await helius.get_signatures(wallet.wallet_address, limit=MAX_SIGS_PER_WALLET)
    if not sigs:
        return 0

    for sig_meta in sigs:
        sig = sig_meta.get("signature")
        if not sig:
            continue
        # Skip if already recorded
        existing = db.query(WalletFundingEdge).filter_by(tx_hash=sig).first()
        if existing:
            continue

        tx = await helius.get_transaction(sig)
        if not tx:
            continue

        recipient, lamports = _parse_sol_transfer(tx, wallet.wallet_address)
        if not recipient or lamports <= 0:
            continue
        sol_amount = Decimal(lamports) / Decimal(LAMPORTS_PER_SOL)
        if float(sol_amount) < MIN_SOL_TRANSFER:
            continue
        if _is_exchange_or_program(recipient):
            continue
        if recipient in known_addrs:
            continue

        # Check freshness
        n_sigs = await _sig_count(recipient)
        if n_sigs > FRESH_WALLET_MAX_SIGS:
            continue

        usd_amount = float(sol_amount) * sol_price if sol_price else 0.0

        # Record edge
        slot = tx.get("slot") or 0
        block_time = tx.get("blockTime")
        funded_dt = (
            datetime.utcfromtimestamp(block_time) if block_time else datetime.utcnow()
        )
        edge = WalletFundingEdge(
            chain="solana",
            funder_address=wallet.wallet_address,
            recipient_address=recipient,
            amount_native=sol_amount,
            amount_usd=Decimal(str(round(usd_amount, 2))),
            tx_hash=sig,
            block_number=int(slot),
            funded_at=funded_dt,
            recipient_nonce_at_funding=n_sigs,
            funder_label=wallet.label,
            funder_role=wallet.role,
        )
        db.add(edge)

        # Auto-add recipient to tracked set
        new_wallet = SolanaKnownWallet(
            wallet_address=recipient,
            label=f"{depth_label} — funded by {wallet.wallet_address[:8]}",
            associated_token=wallet.associated_token,
            associated_mint=wallet.associated_mint,
            role=depth_label,
            funding_source=wallet.wallet_address,
            is_active=True,
            added_at=datetime.utcnow(),
            notes=(
                f"Auto-discovered via graph walk from {wallet.wallet_address[:10]}. "
                f"Received {float(sol_amount):.2f} SOL (${usd_amount:,.0f}). "
                f"Fresh wallet: {n_sigs} prior signatures."
            ),
        )
        db.add(new_wallet)
        known_addrs.add(recipient)
        found += 1
        logger.info(
            f"GRAPH WALK: {wallet.wallet_address[:8]} → {recipient[:8]} "
            f"({float(sol_amount):.1f} SOL, ${usd_amount:,.0f})"
        )

    db.commit()
    return found


async def run_solana_graph_walk():
    """Scheduler entry — every 15 min."""
    if not helius.configured:
        logger.info("solana_graph_walk: HELIUS_API_KEY not set — skipping.")
        return {"skipped": "no_helius_key"}

    logger.info("Starting Solana graph walk run...")
    db = SessionLocal()
    total_found = 0
    try:
        sol_price = await _sol_price_usd()
        known_addrs = {
            w.wallet_address
            for w in db.query(SolanaKnownWallet).filter(
                SolanaKnownWallet.is_active.is_(True)
            ).all()
        }

        # Depth 1: walk cabal_trader wallets → discover cabal_linked
        from sqlalchemy import func as _sql_func
        depth1 = (
            db.query(SolanaKnownWallet)
            .filter(
                SolanaKnownWallet.is_active.is_(True),
                SolanaKnownWallet.role == "cabal_trader",
            )
            .order_by(_sql_func.random())
            .limit(MAX_WALLETS_PER_RUN)
            .all()
        )
        for w in depth1:
            try:
                found = await _walk_wallet(w, db, known_addrs, sol_price, "cabal_linked")
                total_found += found
            except Exception as e:
                logger.error(f"graph walk depth-1 {w.wallet_address[:8]}: {e}")
            finally:
                gc.collect()

        # Depth 2: walk cabal_linked wallets → discover cabal_linked_2
        depth2 = (
            db.query(SolanaKnownWallet)
            .filter(
                SolanaKnownWallet.is_active.is_(True),
                SolanaKnownWallet.role == "cabal_linked",
            )
            .order_by(_sql_func.random())
            .limit(MAX_WALLETS_PER_RUN // 2)  # smaller batch for depth 2
            .all()
        )
        for w in depth2:
            try:
                found = await _walk_wallet(w, db, known_addrs, sol_price, "cabal_linked_2")
                total_found += found
            except Exception as e:
                logger.error(f"graph walk depth-2 {w.wallet_address[:8]}: {e}")
            finally:
                gc.collect()

        logger.info(
            f"Solana graph walk complete. Discovered {total_found} new wallets "
            f"from {len(depth1)} depth-1 + {len(depth2)} depth-2 walks."
        )
    finally:
        db.close()

    return {
        "new_wallets_discovered": total_found,
        "depth1_walked": len(depth1) if 'depth1' in dir() else 0,
        "depth2_walked": len(depth2) if 'depth2' in dir() else 0,
    }
