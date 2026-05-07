"""CEX outflow harvester — discovers wallets recently funded from
target CEX hot wallets, registers them as `cex_funded` tracked wallets.

The downstream pipeline:

  1. THIS job adds new recipients into `solana_known_wallets` with
     role='cex_funded' and funding_source='<exchange>:<hot_idx>'.
  2. `wallet_activity_tracker` (existing, unmodified) picks them up the
     next cycle and starts logging their SPL buys/sells into
     `solana_wallet_activity`.
  3. `accumulation_alerter` joins solana_wallet_activity to
     solana_known_wallets where role='cex_funded', aggregates per
     (mint, wallet) net positions, and fires when N+ wallets cross
     the supply-% threshold on a coin in the target MC band.

Why this is the right shape:
  - Insiders launder fresh wallets through CEX hot wallets to get clean
    SOL. The "wallet that just received SOL from Binance and immediately
    started buying $TOKEN_X" pattern is the leading indicator the user
    described (USDUC: Binance-funded wallets accumulated 5% supply
    weeks before the listing pump).
  - We don't try to detect insiders by watching tokens. We detect
    insiders by watching CEX hot wallets — the funding source is the
    invariant.
"""

import asyncio
import logging
from datetime import datetime
from typing import Iterable

from sqlalchemy.orm import Session

from backend import settings_cache
from backend.clients import helius
from backend.database import SessionLocal
from backend.models.cex_address import CexAddress
from backend.models.solana_known_wallet import SolanaKnownWallet

logger = logging.getLogger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000

# Bound the per-cycle work: # hot wallets × sigs × tx body fetches.
# These caps keep us inside the 8-min job timeout on Render starter.
TX_FETCH_CONCURRENCY = 8
MAX_HOT_WALLETS_PER_CYCLE = 12

# A few well-known program addresses that show up as recipients in CEX
# tx outputs but are NOT user wallets. Skip them to avoid polluting the
# tracked set.
_PROGRAM_BLACKLIST = {
    "11111111111111111111111111111111",                  # System Program
    "ComputeBudget111111111111111111111111111111",       # Compute Budget
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",       # SPL Token
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",      # Associated Token
    "So11111111111111111111111111111111111111112",       # wSOL mint
}


def _cfg(key, default):
    return settings_cache.get(key, default)


def _parse_target_exchanges(raw: str | None) -> set[str]:
    if not raw:
        return {"binance", "okx", "bybit", "bitget"}
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


# ─── Per-tx parsing ──────────────────────────────────────────────────

def _parse_outflows(tx: dict, hot_wallet: str) -> list[dict]:
    """Return [{recipient, sol_amount}] for every account whose SOL
    balance increased in this tx, paired with `hot_wallet` decreasing.

    We use balance deltas (not parsed System::transfer) because CEX
    withdrawals frequently route through Squads or other multi-sig
    programs whose inner instructions don't show as parsed transfers.
    """
    out: list[dict] = []
    try:
        message = (tx.get("transaction") or {}).get("message") or {}
        meta = tx.get("meta") or {}
        if (meta.get("err") is not None):
            return out
        account_keys = message.get("accountKeys") or []
        keys: list[str] = []
        for k in account_keys:
            pk = k.get("pubkey") if isinstance(k, dict) else str(k)
            keys.append(pk or "")
        pre_sol = meta.get("preBalances") or []
        post_sol = meta.get("postBalances") or []
        if len(keys) != len(pre_sol) or len(pre_sol) != len(post_sol):
            return out

        # The hot wallet must be a net SENDER in this tx.
        try:
            hot_idx = keys.index(hot_wallet)
        except ValueError:
            return out
        hot_delta = post_sol[hot_idx] - pre_sol[hot_idx]
        if hot_delta >= 0:
            return out  # not an outflow tx for this hot wallet

        for i, k in enumerate(keys):
            if i == hot_idx or not k:
                continue
            if k in _PROGRAM_BLACKLIST:
                continue
            delta = post_sol[i] - pre_sol[i]
            if delta <= 0:
                continue
            out.append({
                "recipient": k,
                "sol_amount": delta / LAMPORTS_PER_SOL,
            })
    except Exception:
        pass
    return out


async def _harvest_one_hot(
    hot: CexAddress, sigs_per: int, sem: asyncio.Semaphore,
    min_sol: float, max_sol: float,
) -> list[dict]:
    """Pull recent outflows from a single CEX hot wallet."""
    sigs = await helius.get_signatures(hot.address, limit=sigs_per)
    if not sigs:
        return []
    sig_ids = [s.get("signature") for s in sigs if s.get("signature")]

    async def _fetch(sig: str):
        async with sem:
            return await helius.get_transaction(sig)

    txs = await asyncio.gather(*[_fetch(s) for s in sig_ids],
                               return_exceptions=True)
    out: list[dict] = []
    for sig, tx in zip(sig_ids, txs):
        if isinstance(tx, Exception) or not tx:
            continue
        block_time = tx.get("blockTime")
        ts = (
            datetime.utcfromtimestamp(block_time)
            if block_time else datetime.utcnow()
        )
        for ev in _parse_outflows(tx, hot.address):
            if ev["sol_amount"] < min_sol:
                continue
            if ev["sol_amount"] > max_sol:
                continue
            out.append({
                **ev,
                "hot_wallet": hot.address,
                "exchange": (hot.exchange or "unknown").lower(),
                "hot_label": hot.name,
                "signature": sig,
                "ts": ts,
            })
    return out


# ─── Persistence ─────────────────────────────────────────────────────

def _persist_recipients(
    db: Session, events: Iterable[dict],
) -> tuple[int, int]:
    """Upsert each recipient as role='cex_funded'. Returns
    (inserted, refreshed)."""
    inserted = 0
    refreshed = 0
    by_recipient: dict[str, dict] = {}
    for ev in events:
        # Aggregate same-cycle dups, keep largest funding event so the
        # funding_source label reflects the meaningful transfer.
        cur = by_recipient.get(ev["recipient"])
        if cur is None or ev["sol_amount"] > cur["sol_amount"]:
            by_recipient[ev["recipient"]] = ev

    if not by_recipient:
        return (0, 0)

    addrs = list(by_recipient.keys())
    existing = {
        w.wallet_address: w
        for w in db.query(SolanaKnownWallet)
        .filter(SolanaKnownWallet.wallet_address.in_(addrs))
        .all()
    }

    for addr, ev in by_recipient.items():
        funding_source = f"{ev['exchange']}:{ev['hot_label']}"[:64]
        label = f"CEX-funded ({ev['exchange']})"
        notes = (
            f"Funded {ev['sol_amount']:.2f} SOL from {ev['exchange']} "
            f"({ev['hot_label']}) at {ev['ts'].isoformat()}"
        )
        wallet = existing.get(addr)
        if wallet is None:
            db.add(SolanaKnownWallet(
                wallet_address=addr,
                label=label[:255],
                role="cex_funded",
                funding_source=funding_source,
                first_seen_at=ev["ts"],
                added_at=datetime.utcnow(),
                is_active=True,
                notes=notes[:1000],
            ))
            inserted += 1
        else:
            # Refresh funding_source if it was unknown (e.g. wallet
            # was previously added by graph walk as cabal_linked and
            # we now have a stronger CEX-funding signal).
            if not wallet.funding_source:
                wallet.funding_source = funding_source
                refreshed += 1
            if wallet.role and wallet.role.startswith("cabal_linked"):
                # Promote: cabal_linked + CEX-funded is a higher-signal
                # combination than either alone.
                wallet.role = "cex_funded"
                refreshed += 1
    try:
        db.commit()
    except Exception as e:
        logger.error(f"_persist_recipients commit failed: {e}")
        db.rollback()
        return (0, 0)
    return (inserted, refreshed)


# ─── Main entry ──────────────────────────────────────────────────────

async def run_cex_outflow_harvester() -> dict:
    if not helius.configured:
        logger.info("cex_outflow_harvester: HELIUS_API_KEY not set — skipping")
        return {"skipped": "no_helius_key"}
    if not _cfg("CEX_HARVEST_ENABLED", True):
        return {"skipped": "disabled"}

    sigs_per = int(_cfg("CEX_HARVEST_SIGS_PER_HOT", 80))
    min_sol = float(_cfg("CEX_HARVEST_MIN_SOL", 1.0))
    max_sol = float(_cfg("CEX_HARVEST_MAX_SOL", 5000.0))
    targets = _parse_target_exchanges(
        _cfg("CEX_HARVEST_TARGET_EXCHANGES", "binance,okx,bybit,bitget")
    )

    db = SessionLocal()
    try:
        hots = (
            db.query(CexAddress)
            .filter(CexAddress.is_active.is_(True))
            .all()
        )
        hots = [h for h in hots if (h.exchange or "").lower() in targets]
        if not hots:
            return {"skipped": "no_target_hot_wallets",
                    "configured_exchanges": sorted(targets)}

        # Bound work per cycle. Rotate hot wallets by oldest-added so
        # each one gets refreshed over a few cycles.
        hots.sort(key=lambda h: h.added_at or datetime.min)
        hots = hots[:MAX_HOT_WALLETS_PER_CYCLE]

        sem = asyncio.Semaphore(TX_FETCH_CONCURRENCY)
        per_hot = await asyncio.gather(
            *[_harvest_one_hot(h, sigs_per, sem, min_sol, max_sol) for h in hots],
            return_exceptions=True,
        )
        all_events: list[dict] = []
        for r in per_hot:
            if isinstance(r, Exception):
                logger.warning(f"harvest hot wallet failed: {r}")
                continue
            all_events.extend(r)

        inserted, refreshed = _persist_recipients(db, all_events)
        return {
            "hot_wallets_scanned": len(hots),
            "outflow_events": len(all_events),
            "wallets_inserted": inserted,
            "wallets_refreshed": refreshed,
            "exchanges": sorted(targets),
        }
    finally:
        db.close()
