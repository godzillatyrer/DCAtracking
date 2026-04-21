"""
Cabal Wallet Extractor — paste a runner CA, extract early buyers AND
sellers from on-chain transaction history.

Uses Helius RPC to parse the transaction history of a Solana token
mint directly:
  1. Pull up to 1000 signatures on the mint (paginated)
  2. Parse each tx to detect SPL token transfers involving the mint
  3. Per wallet, track: tokens bought, SOL spent (approx), tokens sold,
     SOL received, current balance
  4. Compute realized + unrealized PnL
  5. Filter by profit threshold, rank, return

Captures BOTH still-holding bagholders AND cashed-out cabal members
who rotated profits to fresh wallets. The smartest cabal operators
typically sell near the top — those are the ones we most want to track.

Helius budget per extraction: ~500-1000 calls. At paid tier (50 req/s)
that's ~10-20 seconds per extraction. Acceptable for a manual-trigger
feature that the user calls once per runner.
"""

import asyncio
import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from backend.clients import helius, geckoterminal, gmgn
from backend.database import SessionLocal
from backend.models.solana_known_wallet import SolanaKnownWallet

logger = logging.getLogger(__name__)

# Default extraction filters
DEFAULT_MIN_PROFIT_USD = 5_000.0
DEFAULT_MIN_PROFIT_MULT = 3.0
DEFAULT_MAX_WALLETS = 30
DEFAULT_MAX_SIGNATURES = 1000  # paginate up to this many tx sigs
LAMPORTS_PER_SOL = 1_000_000_000

# Infrastructure addresses to skip (these are programs/pools, not traders)
SKIP_ADDRESSES = {
    "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",    # Raydium AMM v4
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",     # Pump.fun
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",    # Raydium CLMM
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP",    # Orca Whirlpools
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",     # Jupiter v6
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",    # Raydium auth
    "So11111111111111111111111111111111111111112",     # Wrapped SOL
}


async def _get_token_price_usd(mint: str) -> float:
    """Current USD price for the token from GeckoTerminal."""
    try:
        info = await geckoterminal.token_info("solana", mint)
        if info:
            attrs = info.get("attributes") or {}
            return float(attrs.get("price_usd") or 0)
    except Exception:
        pass
    return 0.0


async def _get_sol_price_usd() -> float:
    try:
        from backend.clients import defillama
        prices = await defillama.current_prices(["coingecko:solana"])
        entry = prices.get("coingecko:solana") or {}
        return float(entry.get("price") or 0.0)
    except Exception:
        return 0.0


async def _paginate_signatures(mint: str, max_sigs: int) -> list[dict]:
    """
    Pull up to max_sigs signatures for the mint, paginating via `before`.
    Returns signatures ordered newest → oldest.
    """
    if not helius.configured:
        return []
    all_sigs: list[dict] = []
    before: str | None = None
    while len(all_sigs) < max_sigs:
        params: list = [mint, {"limit": min(200, max_sigs - len(all_sigs))}]
        if before:
            params[1]["before"] = before
        batch = await helius._rpc("getSignaturesForAddress", params)
        if not batch or not isinstance(batch, list):
            break
        all_sigs.extend(batch)
        if len(batch) < 200:
            break
        before = batch[-1].get("signature")
        if not before:
            break
    return all_sigs


def _parse_tx_for_swaps(tx: dict, target_mint: str) -> list[dict]:
    """
    Walk a parsed transaction's instructions and extract per-wallet
    swap events (buy or sell of the target mint).

    Returns list of {wallet, direction, token_amount, sol_amount} where:
      direction = 'buy' (wallet received target_mint)
                  or 'sell' (wallet sent target_mint)
      sol_amount is the SOL side of the swap (approx; Pump.fun pays SOL
      directly, Raydium may route through wSOL)
    """
    events = []
    try:
        message = (tx.get("transaction") or {}).get("message") or {}
        meta = tx.get("meta") or {}

        # account_keys: wallet addresses involved in this tx
        account_keys = message.get("accountKeys") or []
        # In jsonParsed format, accountKeys is list of {"pubkey", "signer", "writable", "source"}
        fee_payer = None
        if account_keys:
            first = account_keys[0]
            fee_payer = first.get("pubkey") if isinstance(first, dict) else str(first)

        # Track SPL token balance changes per owner per mint
        # meta.preTokenBalances / postTokenBalances give this directly
        pre = meta.get("preTokenBalances") or []
        post = meta.get("postTokenBalances") or []

        # Build owner -> balance-change map for the target mint
        pre_by_key = {}
        for b in pre:
            if b.get("mint") != target_mint:
                continue
            owner = b.get("owner")
            if not owner:
                continue
            amt = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            pre_by_key[owner] = amt

        post_by_key = {}
        for b in post:
            if b.get("mint") != target_mint:
                continue
            owner = b.get("owner")
            if not owner:
                continue
            amt = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            post_by_key[owner] = amt

        all_owners = set(pre_by_key) | set(post_by_key)

        # SOL side: preBalances / postBalances are in lamports, by account key index
        # The fee payer's net change (minus tx fee) ≈ SOL spent/received
        pre_sol = meta.get("preBalances") or []
        post_sol = meta.get("postBalances") or []
        sol_delta_lamports = 0
        if pre_sol and post_sol and len(pre_sol) > 0 and len(post_sol) > 0:
            sol_delta_lamports = post_sol[0] - pre_sol[0]  # fee payer balance change
        sol_amount = abs(sol_delta_lamports) / LAMPORTS_PER_SOL

        for owner in all_owners:
            if owner in SKIP_ADDRESSES:
                continue
            pre_amt = pre_by_key.get(owner, 0)
            post_amt = post_by_key.get(owner, 0)
            delta = post_amt - pre_amt
            if abs(delta) < 1e-9:
                continue
            direction = "buy" if delta > 0 else "sell"
            events.append({
                "wallet": owner,
                "direction": direction,
                "token_amount": abs(delta),
                # SOL amount is only accurate for fee_payer; for non-payer
                # swaps it's a rough upper bound.
                "sol_amount": sol_amount if owner == fee_payer else 0.0,
            })
    except Exception as e:
        logger.debug(f"parse_tx error: {e}")
    return events


async def extract_cabal_wallets(
    mint: str,
    *,
    min_profit_usd: float = DEFAULT_MIN_PROFIT_USD,
    min_profit_mult: float = DEFAULT_MIN_PROFIT_MULT,
    max_wallets: int = DEFAULT_MAX_WALLETS,
) -> tuple[list[dict], dict]:
    """
    Main extraction entry point. Returns (wallets, debug_info).

    Each wallet dict has:
      address, total_profit_usd, profit_multiplier, cost_usd,
      balance_usd, balance_tokens, realized_profit_usd,
      unrealized_profit_usd, tx_count, tags, is_smart_money
    """
    debug = {"mint": mint, "source": None}

    if not helius.configured:
        debug["error"] = "HELIUS_API_KEY not configured"
        return [], debug

    token_price_usd = await _get_token_price_usd(mint)
    sol_price_usd = await _get_sol_price_usd()
    debug["token_price_usd"] = token_price_usd
    debug["sol_price_usd"] = sol_price_usd

    sigs = await _paginate_signatures(mint, DEFAULT_MAX_SIGNATURES)
    debug["signatures_pulled"] = len(sigs)
    if not sigs:
        debug["error"] = "no signatures found for mint"
        return [], debug

    # Per-wallet stats
    wallet_stats: dict[str, dict] = {}

    # Parse each transaction. Serial because Helius rate-limits.
    for sig_meta in sigs:
        sig = sig_meta.get("signature")
        if not sig:
            continue
        tx = await helius.get_transaction(sig)
        if not tx:
            continue
        events = _parse_tx_for_swaps(tx, mint)
        for e in events:
            w = e["wallet"]
            s = wallet_stats.setdefault(w, {
                "tokens_bought": 0.0,
                "tokens_sold": 0.0,
                "sol_spent": 0.0,
                "sol_received": 0.0,
                "tx_count": 0,
            })
            s["tx_count"] += 1
            if e["direction"] == "buy":
                s["tokens_bought"] += e["token_amount"]
                s["sol_spent"] += e["sol_amount"]
            else:
                s["tokens_sold"] += e["token_amount"]
                s["sol_received"] += e["sol_amount"]

    debug["wallets_parsed"] = len(wallet_stats)
    debug["source"] = "helius_tx_history"

    # Compute PnL per wallet
    results = []
    for addr, s in wallet_stats.items():
        current_balance = s["tokens_bought"] - s["tokens_sold"]
        current_value_usd = max(0, current_balance) * token_price_usd
        cost_usd = s["sol_spent"] * sol_price_usd
        received_usd = s["sol_received"] * sol_price_usd
        realized = received_usd - cost_usd  # may be negative if still holding cost
        # Realized PnL only from the portion that was sold
        if s["tokens_sold"] > 0 and s["tokens_bought"] > 0:
            avg_cost_per_token_usd = cost_usd / s["tokens_bought"] if s["tokens_bought"] else 0
            realized = received_usd - (avg_cost_per_token_usd * s["tokens_sold"])
        unrealized = current_value_usd - (
            cost_usd * (max(0, current_balance) / s["tokens_bought"])
            if s["tokens_bought"] else 0
        )
        total_profit = realized + unrealized
        profit_mult = (total_profit / cost_usd) if cost_usd > 0 else 0.0

        results.append({
            "address": addr,
            "cost_usd": round(cost_usd, 2),
            "realized_profit_usd": round(realized, 2),
            "unrealized_profit_usd": round(unrealized, 2),
            "total_profit_usd": round(total_profit, 2),
            "profit_multiplier": round(profit_mult, 2),
            "balance_tokens": round(max(0, current_balance), 6),
            "balance_usd": round(current_value_usd, 2),
            "pct_of_supply": 0,
            "tx_count": s["tx_count"],
            "tokens_bought": round(s["tokens_bought"], 6),
            "tokens_sold": round(s["tokens_sold"], 6),
            "is_smart_money": False,
            "tags": ["helius_tx_history"],
            "still_holding": current_balance > 0.0001,
            "fully_exited": s["tokens_sold"] > 0 and current_balance < 0.0001,
            "raw": None,
        })

    # Filter
    filtered = [
        w for w in results
        if w["total_profit_usd"] >= min_profit_usd
        and (w["profit_multiplier"] >= min_profit_mult or w["profit_multiplier"] == 0)
    ]

    # Sort by total profit desc
    filtered.sort(key=lambda x: -x["total_profit_usd"])

    debug["wallets_post_filter"] = len(filtered)
    return filtered[:max_wallets], debug


def add_cabal_wallets_to_db(
    db: Session,
    wallets: list[dict],
    source_mint: str,
    source_symbol: str = "",
) -> int:
    """Insert extracted wallets as role='cabal_trader'. Returns count
    of newly-added wallets (skips duplicates, upgrades role from
    'infrastructure' if seen before)."""
    added = 0
    for w in wallets:
        addr = w["address"]
        existing = db.query(SolanaKnownWallet).filter_by(wallet_address=addr).first()
        if existing:
            if existing.role == "infrastructure":
                existing.role = "cabal_trader"
                existing.associated_mint = source_mint
            continue

        status_tag = "exited" if w.get("fully_exited") else (
            "holding" if w.get("still_holding") else "active"
        )
        label_parts = [f"${w['total_profit_usd']:,.0f}"]
        if w["profit_multiplier"] > 0:
            label_parts.append(f"{w['profit_multiplier']:.1f}x")
        label_parts.append(status_tag)

        wallet = SolanaKnownWallet(
            wallet_address=addr,
            label=f"{source_symbol or 'runner'} — {', '.join(label_parts)}",
            associated_token=source_symbol or None,
            associated_mint=source_mint,
            role="cabal_trader",
            is_active=True,
            total_profit_est=Decimal(str(round(w["total_profit_usd"], 2))),
            added_at=datetime.utcnow(),
            notes=(
                f"Extracted from {source_symbol or source_mint[:10]}. "
                f"Realized ${w['realized_profit_usd']:,.0f}, "
                f"unrealized ${w['unrealized_profit_usd']:,.0f}. "
                f"Status: {status_tag}. Tx count: {w['tx_count']}."
            ),
        )
        db.add(wallet)
        added += 1

    db.commit()
    return added
