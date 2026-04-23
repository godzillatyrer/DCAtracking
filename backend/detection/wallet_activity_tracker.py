"""
Solana Wallet Activity Tracker — polls tracked wallets for recent
SPL swaps.

For each active wallet in `solana_known_wallets`, fetch the last N
signatures via Helius, parse SPL token balance changes, and persist
rows to `solana_wallet_activity`. The frontend Live Activity page
queries this table to surface:
  - recent buys across the whole tracked set
  - convergence: mints that multiple tracked wallets bought in a window

Idempotent per (wallet, signature). Safe to re-run.
"""

import asyncio
import gc
import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from backend.clients import defillama, helius
from backend.database import SessionLocal
from backend.models.cex_address import CexAddress
from backend.models.solana_known_wallet import SolanaKnownWallet
from backend.models.solana_wallet_activity import SolanaWalletActivity

logger = logging.getLogger(__name__)

# How many wallets to scan per run. With 100-500 tracked wallets and
# ~15 sigs each, we cap to keep a run inside the job timeout. Rotated
# across runs so everyone gets scanned over ~30 min.
MAX_WALLETS_PER_RUN = 60
MAX_SIGS_PER_WALLET = 15
# Run-wide parallelism. Helius free tier is ~10 req/sec; we fan out
# signatures across many wallets with a single shared semaphore so
# one huge wallet can't starve the others.
HELIUS_CONCURRENCY = 10
LAMPORTS_PER_SOL = 1_000_000_000

# SPL mints we never log as "tokens" — infra, wrapped SOL, etc.
_SKIP_MINTS = {
    "So11111111111111111111111111111111111111112",  # wSOL
}


def _parse_sol_transfers_to_cex(tx: dict, cex_set: set[str], sender: str) -> list[dict]:
    """Extract System::transfer instructions whose destination is a
    known CEX deposit address. Returns a list of {recipient, amount_sol}.
    """
    out = []
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
            dest = info.get("destination") or ""
            if dest not in cex_set:
                continue
            lamports = int(info.get("lamports") or 0)
            if lamports <= 0:
                continue
            out.append({
                "recipient": dest, "sol_amount": lamports / LAMPORTS_PER_SOL,
            })
    except Exception:
        pass
    return out


def _parse_swaps(tx: dict) -> list[dict]:
    """Return a list of (owner, mint, token_delta, sol_amount) entries
    for every wallet whose SPL balance changed in this tx."""
    events = []
    try:
        message = (tx.get("transaction") or {}).get("message") or {}
        meta = tx.get("meta") or {}
        account_keys = message.get("accountKeys") or []

        key_index: dict[str, int] = {}
        for i, k in enumerate(account_keys):
            pubkey = k.get("pubkey") if isinstance(k, dict) else str(k)
            if pubkey:
                key_index[pubkey] = i

        pre = meta.get("preTokenBalances") or []
        post = meta.get("postTokenBalances") or []

        # (owner, mint) -> amount
        pre_map: dict[tuple[str, str], float] = {}
        for b in pre:
            owner = b.get("owner")
            mint = b.get("mint")
            if not owner or not mint:
                continue
            pre_map[(owner, mint)] = float(
                (b.get("uiTokenAmount") or {}).get("uiAmount") or 0
            )
        post_map: dict[tuple[str, str], float] = {}
        for b in post:
            owner = b.get("owner")
            mint = b.get("mint")
            if not owner or not mint:
                continue
            post_map[(owner, mint)] = float(
                (b.get("uiTokenAmount") or {}).get("uiAmount") or 0
            )

        all_keys = set(pre_map) | set(post_map)
        pre_sol = meta.get("preBalances") or []
        post_sol = meta.get("postBalances") or []

        for (owner, mint) in all_keys:
            if mint in _SKIP_MINTS:
                continue
            delta = post_map.get((owner, mint), 0) - pre_map.get((owner, mint), 0)
            if abs(delta) < 1e-9:
                continue

            sol_amount = 0.0
            idx = key_index.get(owner)
            if idx is not None and idx < len(pre_sol) and idx < len(post_sol):
                sol_amount = abs(post_sol[idx] - pre_sol[idx]) / LAMPORTS_PER_SOL

            events.append({
                "owner": owner,
                "mint": mint,
                "direction": "buy" if delta > 0 else "sell",
                "token_amount": abs(delta),
                "sol_amount": sol_amount,
            })
    except Exception:
        pass
    return events


async def _scan_one_wallet(
    wallet: SolanaKnownWallet,
    sem: asyncio.Semaphore,
    sol_price_usd: float,
    existing_mints_by_wallet: dict[str, set[str]],
    cex_set: set[str],
) -> list[SolanaWalletActivity]:
    """Pull last N signatures for one wallet, parse, return new
    activity rows (not yet committed)."""
    new_rows: list[SolanaWalletActivity] = []
    sigs = await helius.get_signatures(
        wallet.wallet_address, limit=MAX_SIGS_PER_WALLET
    )
    if not sigs:
        return new_rows

    async def _fetch(sig: str) -> dict | None:
        async with sem:
            return await helius.get_transaction(sig)

    sig_ids = [s.get("signature") for s in sigs if s.get("signature")]
    txs = await asyncio.gather(
        *[_fetch(s) for s in sig_ids], return_exceptions=True
    )

    known_mints = existing_mints_by_wallet.get(wallet.wallet_address, set())
    seen_sigs_this_run: set[str] = set()

    for sig, tx in zip(sig_ids, txs):
        if not tx or isinstance(tx, Exception):
            continue
        block_time = tx.get("blockTime")
        detected = (
            datetime.utcfromtimestamp(block_time) if block_time else datetime.utcnow()
        )
        slot = int(tx.get("slot") or 0)

        # CEX outflows (SOL transfers to known exchange deposit addrs)
        if cex_set:
            for cex_ev in _parse_sol_transfers_to_cex(tx, cex_set, wallet.wallet_address):
                key = f"{sig}:cex:{cex_ev['recipient']}"
                if key in seen_sigs_this_run:
                    continue
                seen_sigs_this_run.add(key)
                value_usd = cex_ev["sol_amount"] * sol_price_usd if sol_price_usd else 0.0
                new_rows.append(SolanaWalletActivity(
                    wallet_address=wallet.wallet_address,
                    activity_type="cex_outflow",
                    token_mint=None,
                    token_symbol=None,
                    amount=Decimal(str(round(cex_ev["sol_amount"], 6))),
                    value_usd=Decimal(str(round(value_usd, 2))),
                    counterparty=cex_ev["recipient"],
                    signature=sig, slot=slot,
                    detected_at=detected,
                    is_new_token=False, flagged=True,
                ))

        for e in _parse_swaps(tx):
            if e["owner"] != wallet.wallet_address:
                continue
            # Dedup within the same run (same sig can report multiple
            # mint rows, but we only want per-mint entries).
            key = f"{sig}:{e['mint']}"
            if key in seen_sigs_this_run:
                continue
            seen_sigs_this_run.add(key)

            is_new = e["mint"] not in known_mints and e["direction"] == "buy"
            value_usd = (e["sol_amount"] * sol_price_usd) if sol_price_usd else 0.0

            new_rows.append(SolanaWalletActivity(
                wallet_address=wallet.wallet_address,
                activity_type="spl_buy" if e["direction"] == "buy" else "spl_sell",
                token_mint=e["mint"],
                token_symbol=None,  # resolved lazily on display
                amount=Decimal(str(round(e["token_amount"], 6))),
                value_usd=Decimal(str(round(value_usd, 2))),
                signature=sig,
                slot=slot,
                detected_at=detected,
                is_new_token=is_new,
                flagged=is_new,
            ))

    return new_rows


async def run_wallet_activity_tracker():
    """Scheduler entry — every 5 min."""
    if not helius.configured:
        logger.info("wallet_activity_tracker: HELIUS_API_KEY not set — skipping")
        return {"skipped": "no_helius_key"}

    db = SessionLocal()
    total_added = 0
    wallets_scanned = 0
    try:
        # SOL price for value_usd calc
        try:
            prices = await defillama.current_prices(["coingecko:solana"])
            sol_price_usd = float(
                (prices.get("coingecko:solana") or {}).get("price") or 0
            )
        except Exception:
            sol_price_usd = 0.0

        # Pick the least-recently-scanned wallets. We approximate
        # "least-recent" by ordering by the max detected_at per wallet,
        # NULLS first so newcomers jump to the front of the queue.
        from sqlalchemy import func, desc

        subq = (
            db.query(
                SolanaWalletActivity.wallet_address.label("w"),
                func.max(SolanaWalletActivity.detected_at).label("last_seen"),
            )
            .group_by(SolanaWalletActivity.wallet_address)
            .subquery()
        )
        wallets = (
            db.query(SolanaKnownWallet)
            .outerjoin(subq, subq.c.w == SolanaKnownWallet.wallet_address)
            .filter(SolanaKnownWallet.is_active.is_(True))
            .order_by(subq.c.last_seen.asc().nullsfirst())
            .limit(MAX_WALLETS_PER_RUN)
            .all()
        )

        # Known CEX deposit addresses
        cex_set: set[str] = {
            r[0] for r in db.query(CexAddress.address)
            .filter(CexAddress.is_active.is_(True)).all()
        }

        # Pre-load each wallet's known mints so we can flag is_new_token.
        known_map: dict[str, set[str]] = {}
        for w in wallets:
            mints = {
                m for (m,) in db.query(SolanaWalletActivity.token_mint)
                .filter(SolanaWalletActivity.wallet_address == w.wallet_address)
                .distinct()
                .all()
                if m
            }
            known_map[w.wallet_address] = mints

        # Dedup against existing signatures to avoid re-inserting.
        sem = asyncio.Semaphore(HELIUS_CONCURRENCY)
        for w in wallets:
            try:
                new_rows = await _scan_one_wallet(w, sem, sol_price_usd, known_map, cex_set)
                if new_rows:
                    existing_sigs = {
                        s for (s,) in db.query(SolanaWalletActivity.signature)
                        .filter(SolanaWalletActivity.wallet_address == w.wallet_address)
                        .filter(SolanaWalletActivity.signature.in_([r.signature for r in new_rows]))
                        .all()
                    }
                    for r in new_rows:
                        if r.signature in existing_sigs:
                            continue
                        db.add(r)
                        total_added += 1
                    db.commit()
                wallets_scanned += 1
            except Exception as e:
                logger.error(f"activity scan {w.wallet_address[:8]}: {e}")
                db.rollback()
            finally:
                gc.collect()

        logger.info(
            f"wallet_activity_tracker: scanned {wallets_scanned} wallets, "
            f"added {total_added} activity rows."
        )
    finally:
        db.close()

    return {
        "wallets_scanned": wallets_scanned,
        "activities_added": total_added,
    }
