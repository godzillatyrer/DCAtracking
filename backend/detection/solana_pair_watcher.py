"""
Solana Pair Watcher — Modules 4 / 5 / 9 on Solana.

Polls GeckoTerminal `/networks/solana/new_pools` and emits the same
launch_signal types as the BSC pair_watcher:

  big_initial_lp        (FILTER) — initial LP >= BIG_LP_MIN_USD
  insider_early_buyer   (HIGH)   — ≥N solana_known_wallets in first buyers
                                   (resolved via GMGN smart-money trades)
  known_lp_provider     (HIGH)   — initial LP provider is a known Solana wallet

Solana doesn't have BSC's `eth_getLogs` model, so for buyer detection we
rely on GMGN's per-token `smart_money_trades` endpoint. For LP provider
identification we read the Raydium/Orca pool-init transaction via
Helius.

Memory-disciplined: 25 pools max per run, gc.collect after each pool.
"""

import gc
import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from backend.clients import geckoterminal, gmgn, helius
from backend.config import settings
from backend.database import SessionLocal
from backend.detection.launch_scorer import record_signal
from backend.models.launch_signal import TIER_FILTER, TIER_HIGH
from backend.models.pair_creation import PairCreation
from backend.models.solana_known_wallet import SolanaKnownWallet

logger = logging.getLogger(__name__)

MAX_POOLS_PER_RUN = 25


def _extract_meta(pool: dict) -> dict | None:
    try:
        attrs = pool.get("attributes") or {}
        rels = pool.get("relationships") or {}
        base_id = ((rels.get("base_token") or {}).get("data") or {}).get("id", "")
        quote_id = ((rels.get("quote_token") or {}).get("data") or {}).get("id", "")
        # GeckoTerminal IDs are "solana_<mint>"
        base_addr = base_id.split("_", 1)[-1] if base_id else ""
        quote_addr = quote_id.split("_", 1)[-1] if quote_id else ""
        if not base_addr:
            return None
        try:
            lp_usd = float(attrs.get("reserve_in_usd") or 0)
        except Exception:
            lp_usd = 0.0
        created = attrs.get("pool_created_at")
        created_dt = None
        if created:
            try:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except Exception:
                pass
        # GeckoTerminal pool name on Solana is "BASE / QUOTE" (e.g.
        # "WIF / SOL"). Split to recover the base symbol so the Launches
        # dashboard renders something better than "—".
        pool_name = attrs.get("name") or ""
        base_symbol = None
        if "/" in pool_name:
            base_symbol = pool_name.split("/", 1)[0].strip() or None
        return {
            "dex": (attrs.get("dex") or {}).get("name") if isinstance(attrs.get("dex"), dict) else attrs.get("dex_id"),
            "pair_address": attrs.get("address") or "",
            "base_token": base_addr,
            "quote_token": quote_addr,
            "base_symbol": base_symbol,
            "pool_name": pool_name or None,
            "initial_lp_usd": lp_usd,
            "created_at": created_dt,
        }
    except Exception:
        return None


def _load_known(db: Session) -> dict[str, SolanaKnownWallet]:
    rows = db.query(SolanaKnownWallet).filter(SolanaKnownWallet.is_active.is_(True)).all()
    return {w.wallet_address: w for w in rows}


async def _identify_lp_provider(pair_address: str) -> str | None:
    """
    Solana: read the pool's first signature via Helius and find the
    account that funded the initial liquidity. We do a coarse approach —
    pick the fee payer of the earliest signature for this account. It's
    not perfect but it's correct often enough to flag known LP providers.
    """
    if not helius.configured:
        return None
    sigs = await helius.get_signatures(pair_address, limit=1)
    if not sigs:
        return None
    earliest = sigs[-1]
    sig = earliest.get("signature")
    if not sig:
        return None
    tx = await helius.get_transaction(sig)
    if not tx:
        return None
    try:
        message = ((tx.get("transaction") or {}).get("message") or {})
        keys = message.get("accountKeys") or []
        if not keys:
            return None
        first = keys[0]
        # jsonParsed returns dicts {pubkey, signer, ...}
        if isinstance(first, dict):
            return first.get("pubkey")
        return str(first)
    except Exception:
        return None


async def _process_pool(db: Session, meta: dict, known: dict[str, SolanaKnownWallet]):
    pair_addr = meta["pair_address"]
    base_addr = meta["base_token"]
    if not pair_addr or not base_addr:
        return

    existing = db.query(PairCreation).filter_by(
        chain="solana", pair_address=pair_addr
    ).first()
    if existing is None:
        row = PairCreation(
            chain="solana",
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

    lp_usd = float(existing.initial_lp_usd or 0)
    sym_kwargs = {
        "token_symbol": meta.get("base_symbol"),
        "token_name": meta.get("pool_name"),
    }

    # Module 4: Big Initial LP (FILTER)
    if lp_usd >= settings.BIG_LP_MIN_USD:
        record_signal(
            db,
            chain="solana",
            contract_address=base_addr,
            signal_type="big_initial_lp",
            signal_tier=TIER_FILTER,
            confidence=60,
            evidence={"pair": pair_addr, "initial_lp_usd": lp_usd},
            description=f"Solana pool initial LP ${lp_usd:,.0f} on {meta.get('dex') or 'DEX'}.",
            initial_lp_usd=lp_usd,
            **sym_kwargs,
        )

    # Module 9: Known LP Provider (HIGH)
    if not existing.lp_provider and helius.configured:
        provider = await _identify_lp_provider(pair_addr)
        if provider:
            existing.lp_provider = provider
            db.commit()
    if existing.lp_provider and existing.lp_provider in known:
        kw = known[existing.lp_provider]
        record_signal(
            db,
            chain="solana",
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
            description=f"Solana pool LP seeded by known operator: {kw.label}",
            **sym_kwargs,
        )

    # Module 5: Insider Early-Buyer Cluster (HIGH)
    # GMGN provides smart-money trades for any token mint
    if not existing.first_buys_processed:
        trades = await gmgn.smart_money_trades(base_addr)
        known_buyers = []
        if isinstance(trades, list):
            for t in trades[:60]:
                wallet = (
                    t.get("wallet")
                    or t.get("buyer")
                    or t.get("address")
                    or ""
                )
                if not wallet:
                    continue
                if wallet in known:
                    known_buyers.append(wallet)
        existing.first_buys_processed = True
        existing.insider_buyer_count = len(known_buyers)
        if len(known_buyers) >= settings.INSIDER_EARLY_BUYER_MIN_COUNT:
            record_signal(
                db,
                chain="solana",
                contract_address=base_addr,
                signal_type="insider_early_buyer",
                signal_tier=TIER_HIGH,
                confidence=min(95, 60 + 5 * len(known_buyers)),
                evidence={
                    "known_buyers": known_buyers,
                    "via": "gmgn_smart_money_trades",
                    "pair": pair_addr,
                },
                description=(
                    f"{len(known_buyers)} labeled Solana operators among "
                    f"the smart-money buyers of this token."
                ),
                **sym_kwargs,
            )
        db.commit()


async def run_solana_pair_watcher():
    if not helius.configured:
        logger.info("solana_pair_watcher: HELIUS_API_KEY not set — running with GMGN only.")
    logger.info("Starting Solana pair watcher run...")
    db = SessionLocal()
    processed = 0
    try:
        pools = await geckoterminal.new_pools("solana", limit=MAX_POOLS_PER_RUN)
        known = _load_known(db)
        for pool in pools:
            meta = _extract_meta(pool)
            if not meta or not meta.get("pair_address"):
                continue
            try:
                await _process_pool(db, meta, known)
                processed += 1
            except Exception as e:
                logger.error(f"solana pool process error: {e}")
            finally:
                gc.collect()
        logger.info(f"Solana pair watcher complete. Processed {processed} new pools.")
    finally:
        db.close()
    return {"pools_processed": processed, "chain": "solana"}
