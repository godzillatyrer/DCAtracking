"""
Modules 4 / 5 / 9 — Pair Watcher.

Polls GeckoTerminal + DEX Screener for newly-created pools/pairs,
records PairCreation rows, and emits signals:

  Module 4 (Big Initial LP — filter tier):
    Fires when a brand-new pair has initial liquidity >= BIG_LP_MIN_USD.
    Filter tier: never triggers alone.

  Module 5 (Insider Early-Buyer Cluster — high tier):
    Fires when ≥ INSIDER_EARLY_BUYER_MIN_COUNT of our `known_wallets`
    show up among the first N buyers of the new pair.

  Module 9 (Known LP Provider — high tier):
    Fires when the wallet that seeded initial liquidity matches a
    known operator / launcher. We identify this from the pool's first
    Mint event (LP token mint to the LP provider).

The watcher is BSC-first for now — Solana pools come in via a follow-up
that consumes geckoterminal `/networks/solana/new_pools` identically.
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Iterable

from sqlalchemy.orm import Session

from backend.bscscan_client import (
    TRANSFER_TOPIC,
    _chunked_get_logs,
    _hex_to_int,
    _pad_address,
    _rpc_call,
    _unpad_address,
)
from backend.clients import geckoterminal
from backend.config import settings
from backend.database import SessionLocal
from backend.detection.launch_scorer import record_signal
from backend.models.known_wallet import KnownWallet
from backend.models.launch_signal import TIER_FILTER, TIER_HIGH
from backend.models.pair_creation import PairCreation

logger = logging.getLogger(__name__)

# PancakeSwap V2 factory + pool-create event
PANCAKE_V2_FACTORY = "0xca143ce32fe78f1f7019d7d551a6402fc5350c73"


def _extract_pair_meta(pool: dict) -> dict | None:
    """Map GeckoTerminal pool response → our PairCreation fields."""
    try:
        attrs = pool.get("attributes") or {}
        rels = pool.get("relationships") or {}
        base_id = ((rels.get("base_token") or {}).get("data") or {}).get("id", "")
        quote_id = ((rels.get("quote_token") or {}).get("data") or {}).get("id", "")
        # base_id is like "bsc_0xabc"; split off the chain prefix
        base_addr = base_id.split("_", 1)[-1] if base_id else ""
        quote_addr = quote_id.split("_", 1)[-1] if quote_id else ""
        reserve_usd = attrs.get("reserve_in_usd")
        try:
            lp_usd = float(reserve_usd) if reserve_usd is not None else 0.0
        except Exception:
            lp_usd = 0.0
        pool_created = attrs.get("pool_created_at")
        created_at = None
        if pool_created:
            try:
                created_at = datetime.fromisoformat(pool_created.replace("Z", "+00:00"))
            except Exception:
                pass
        return {
            "dex": (attrs.get("dex") or {}).get("name") if isinstance(attrs.get("dex"), dict) else attrs.get("dex_id"),
            "pair_address": (attrs.get("address") or "").lower(),
            "base_token": base_addr.lower(),
            "quote_token": quote_addr.lower(),
            "initial_lp_usd": lp_usd,
            "created_at": created_at,
        }
    except Exception as e:
        logger.debug(f"extract_pair_meta error: {e}")
        return None


async def _identify_lp_provider(pair_addr: str) -> str | None:
    """
    Find the LP provider by looking for the first mint of LP tokens
    from the pair's own contract (for Uniswap-V2-style pools, minting
    LP tokens = Transfer from 0x0).

    We scan a narrow window of blocks near pool creation to avoid huge
    log sweeps.
    """
    logs = await _chunked_get_logs(
        {
            "address": pair_addr.lower(),
            "topics": [TRANSFER_TOPIC, "0x" + "0" * 64],
        },
        total_blocks=100_000,
    )
    if not logs:
        return None
    first = logs[0]
    topics = first.get("topics") or []
    if len(topics) < 3:
        return None
    return _unpad_address(topics[2]).lower()


async def _first_buyers(pair_addr: str, base_token: str, block_window: int) -> list[str]:
    """
    Return the list of unique buyer addresses in the first `block_window`
    blocks after pool creation.

    We look at Transfer events of the base_token coming OUT of the pair
    (i.e. someone bought the token from the pool).
    """
    logs = await _chunked_get_logs(
        {
            "address": base_token.lower(),
            "topics": [TRANSFER_TOPIC, _pad_address(pair_addr)],
        },
        total_blocks=block_window,
    )
    buyers: list[str] = []
    seen = set()
    for log in logs:
        topics = log.get("topics") or []
        if len(topics) < 3:
            continue
        buyer = _unpad_address(topics[2]).lower()
        if buyer in seen:
            continue
        seen.add(buyer)
        buyers.append(buyer)
    return buyers


def _load_known_addresses(db: Session) -> dict[str, KnownWallet]:
    rows = db.query(KnownWallet).filter(KnownWallet.is_active.is_(True)).all()
    return {w.wallet_address.lower(): w for w in rows}


async def _process_new_pair(db: Session, meta: dict, known: dict[str, KnownWallet]):
    pair_addr = meta["pair_address"]
    base_addr = meta["base_token"]
    if not pair_addr or not base_addr:
        return

    # Upsert PairCreation
    existing = db.query(PairCreation).filter_by(
        chain="bsc", pair_address=pair_addr
    ).first()
    if existing is None:
        row = PairCreation(
            chain="bsc",
            dex=meta.get("dex"),
            pair_address=pair_addr,
            base_token=base_addr,
            quote_token=meta.get("quote_token"),
            initial_lp_usd=Decimal(str(meta.get("initial_lp_usd") or 0.0)),
            created_at=meta.get("created_at"),
            detected_at=datetime.utcnow(),
        )
        db.add(row)
        db.commit()
        existing = row

    # Module 4: Big Initial LP (filter tier)
    lp_usd = float(existing.initial_lp_usd or 0)
    if lp_usd >= settings.BIG_LP_MIN_USD:
        record_signal(
            db,
            chain="bsc",
            contract_address=base_addr,
            signal_type="big_initial_lp",
            signal_tier=TIER_FILTER,
            confidence=60,
            evidence={"pair": pair_addr, "initial_lp_usd": lp_usd},
            description=f"Initial LP ${lp_usd:,.0f} on {meta.get('dex') or 'BSC DEX'}.",
            initial_lp_usd=lp_usd,
        )

    # Module 9: Known LP Provider (high tier)
    if not existing.lp_provider:
        provider = await _identify_lp_provider(pair_addr)
        if provider:
            existing.lp_provider = provider
            db.commit()
    if existing.lp_provider and existing.lp_provider in known:
        kw = known[existing.lp_provider]
        record_signal(
            db,
            chain="bsc",
            contract_address=base_addr,
            signal_type="known_lp_provider",
            signal_tier=TIER_HIGH,
            confidence=80,
            evidence={
                "lp_provider": existing.lp_provider,
                "label": kw.label,
                "role": kw.role,
                "pair": pair_addr,
            },
            description=f"Initial LP seeded by known operator: {kw.label}",
        )

    # Module 5: Insider Early-Buyer Cluster (high tier)
    if not existing.first_buys_processed:
        buyers = await _first_buyers(
            pair_addr, base_addr, settings.INSIDER_EARLY_BUYER_BLOCK_WINDOW
        )
        known_buyers = [b for b in buyers if b in known]
        existing.first_buys_processed = True
        existing.insider_buyer_count = len(known_buyers)
        if len(known_buyers) >= settings.INSIDER_EARLY_BUYER_MIN_COUNT:
            record_signal(
                db,
                chain="bsc",
                contract_address=base_addr,
                signal_type="insider_early_buyer",
                signal_tier=TIER_HIGH,
                confidence=min(95, 60 + 5 * len(known_buyers)),
                evidence={
                    "known_buyers": known_buyers,
                    "total_first_buyers": len(buyers),
                    "pair": pair_addr,
                },
                description=(
                    f"{len(known_buyers)} labeled wallets appear in the first "
                    f"{settings.INSIDER_EARLY_BUYER_BLOCK_WINDOW} blocks."
                ),
            )
        db.commit()


async def run_pair_watcher():
    """Scheduler entry — every PAIR_WATCHER_INTERVAL_MIN minutes."""
    logger.info("Starting pair watcher run...")
    db = SessionLocal()
    processed = 0
    try:
        # BSC new pools
        pools = await geckoterminal.new_pools("bsc", limit=50)
        known = _load_known_addresses(db)
        for pool in pools:
            meta = _extract_pair_meta(pool)
            if not meta or not meta.get("pair_address"):
                continue
            try:
                await _process_new_pair(db, meta, known)
                processed += 1
            except Exception as e:
                logger.error(f"pair process error for {meta.get('pair_address')}: {e}")
        logger.info(f"Pair watcher complete. Processed {processed} new pairs.")
    finally:
        db.close()
    return {"pairs_processed": processed}
