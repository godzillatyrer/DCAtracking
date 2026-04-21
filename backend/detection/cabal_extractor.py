"""
Cabal Wallet Extractor — multi-source.

Given a Solana runner CA, pull early buyers + sellers + top holders
from every data source we have access to, merge + dedup by wallet,
then insert into solana_known_wallets.

Sources (merged in priority order, richest-data-wins on dup):
  1. GMGN /tokens/top_holders/sol/{mint}   — current top holders w/ balance
  2. GMGN /tokens/top_traders/sol/{mint}   — top traders ranked by PnL
  3. Helius getTokenLargestAccounts + account-info resolution — on-chain
     truth for top 20 current holders (regardless of GMGN)
  4. Helius transaction-history parse — captures sellers who already
     exited (zero balance but realized PnL)

Filtering is deliberately PERMISSIVE by default:
  - min_profit_usd=500 (was 5000) — avoids missing small-but-real winners
  - min_profit_mult=0 — many wallets lack reliable PnL; we keep them
                       anyway if they appear in a credible source

User can tighten via request params. Over time, wallets that keep
appearing across multiple runners are the real cabal — those are
the strongest signal.
"""

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from backend.clients import defillama, geckoterminal, gmgn, helius
from backend.database import SessionLocal
from backend.models.solana_known_wallet import SolanaKnownWallet

logger = logging.getLogger(__name__)

DEFAULT_MIN_PROFIT_USD = 500.0
DEFAULT_MIN_PROFIT_MULT = 0.0
DEFAULT_MAX_WALLETS = 60
DEFAULT_MAX_SIGNATURES = 600
LAMPORTS_PER_SOL = 1_000_000_000

SKIP_ADDRESSES = {
    "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
    "So11111111111111111111111111111111111111112",
}


def _empty_wallet(addr: str) -> dict:
    return {
        "address": addr,
        "cost_usd": 0,
        "realized_profit_usd": 0,
        "unrealized_profit_usd": 0,
        "total_profit_usd": 0,
        "profit_multiplier": 0,
        "balance_tokens": 0,
        "balance_usd": 0,
        "pct_of_supply": 0,
        "tx_count": 0,
        "tokens_bought": 0,
        "tokens_sold": 0,
        "is_smart_money": False,
        "tags": [],
        "still_holding": False,
        "fully_exited": False,
        "sources": [],
    }


def _merge_into(pool: dict[str, dict], addr: str, new: dict, source: str):
    """Merge a new wallet record into the pool, keeping the richest value per field."""
    if addr in SKIP_ADDRESSES or not addr or len(addr) < 20:
        return
    existing = pool.get(addr) or _empty_wallet(addr)
    if source not in existing["sources"]:
        existing["sources"].append(source)
    if source not in existing["tags"]:
        existing["tags"].append(source)
    # For each field, keep larger absolute value (covers PnL sign)
    for key in (
        "cost_usd", "realized_profit_usd", "unrealized_profit_usd",
        "total_profit_usd", "balance_tokens", "balance_usd",
        "pct_of_supply", "tx_count", "tokens_bought", "tokens_sold",
    ):
        if abs(new.get(key) or 0) > abs(existing.get(key) or 0):
            existing[key] = new[key]
    if new.get("profit_multiplier") and new["profit_multiplier"] > existing["profit_multiplier"]:
        existing["profit_multiplier"] = new["profit_multiplier"]
    if new.get("is_smart_money"):
        existing["is_smart_money"] = True
    if new.get("still_holding"):
        existing["still_holding"] = True
    if new.get("fully_exited"):
        existing["fully_exited"] = True
    # Merge extra tags
    for t in new.get("tags") or []:
        if t and t not in existing["tags"]:
            existing["tags"].append(t)
    pool[addr] = existing


# ─── Source 1 + 2: GMGN ────────────────────────────────────────────────

def _extract_gmgn_wallet(raw: dict) -> tuple[str | None, dict]:
    addr = (
        raw.get("address") or raw.get("wallet_address") or raw.get("wallet")
        or raw.get("holder_address") or raw.get("trader_address") or ""
    )
    if not addr:
        return None, {}
    w = _empty_wallet(addr)
    w["realized_profit_usd"] = float(raw.get("realized_profit") or raw.get("realized_pnl") or 0) or 0
    w["unrealized_profit_usd"] = float(raw.get("unrealized_profit") or raw.get("unrealized_pnl") or 0) or 0
    w["total_profit_usd"] = w["realized_profit_usd"] + w["unrealized_profit_usd"]
    if w["total_profit_usd"] == 0:
        w["total_profit_usd"] = float(raw.get("profit") or raw.get("total_profit") or 0) or 0
    w["cost_usd"] = float(raw.get("cost") or raw.get("total_cost") or raw.get("buy_amount_cur") or raw.get("buy_volume") or 0) or 0
    if w["cost_usd"] > 0 and w["total_profit_usd"]:
        w["profit_multiplier"] = round(w["total_profit_usd"] / w["cost_usd"], 2)
    w["balance_tokens"] = float(raw.get("balance") or raw.get("amount") or raw.get("token_balance") or 0) or 0
    w["balance_usd"] = float(raw.get("value") or raw.get("usd_value") or raw.get("balance_usd") or 0) or 0
    w["pct_of_supply"] = float(raw.get("percentage") or raw.get("holder_percentage") or 0) or 0
    w["is_smart_money"] = bool(raw.get("is_smart_money") or raw.get("smart_money"))
    tags = raw.get("tags") or raw.get("labels") or []
    if isinstance(tags, str):
        tags = [tags]
    w["tags"] = [t for t in (tags or []) if t]
    w["still_holding"] = w["balance_tokens"] > 0
    w["fully_exited"] = w["balance_tokens"] == 0 and w["realized_profit_usd"] != 0
    return addr, w


async def _collect_from_gmgn(mint: str, pool: dict, debug: dict):
    """Pull from both GMGN top_traders AND top_holders."""
    try:
        traders = await gmgn.token_top_traders(mint, limit=100) or []
        items = _unwrap(traders)
        debug["gmgn_top_traders_count"] = len(items)
        for raw in items:
            if not isinstance(raw, dict):
                continue
            addr, w = _extract_gmgn_wallet(raw)
            if addr:
                _merge_into(pool, addr, w, "gmgn_trader")
    except Exception as e:
        debug["gmgn_top_traders_error"] = str(e)[:200]

    try:
        holders = await gmgn.token_holders(mint, limit=100) or []
        items = _unwrap(holders)
        debug["gmgn_top_holders_count"] = len(items)
        for raw in items:
            if not isinstance(raw, dict):
                continue
            addr, w = _extract_gmgn_wallet(raw)
            if addr:
                _merge_into(pool, addr, w, "gmgn_holder")
    except Exception as e:
        debug["gmgn_top_holders_error"] = str(e)[:200]


def _unwrap(data: Any) -> list[dict]:
    """Unwrap GMGN's variable response shapes to a list of dicts."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in ("data", "items", "holders", "traders", "list", "results"):
            v = data.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


# ─── Source 3: Helius getTokenLargestAccounts ──────────────────────────

async def _collect_helius_top_holders(mint: str, pool: dict, debug: dict):
    if not helius.configured:
        debug["helius_top_holders"] = "HELIUS_API_KEY not set"
        return
    try:
        price_usd = 0.0
        info = await geckoterminal.token_info("solana", mint)
        if info:
            attrs = info.get("attributes") or {}
            price_usd = float(attrs.get("price_usd") or 0)

        result = await helius._rpc("getTokenLargestAccounts", [mint])
        if not result or not isinstance(result, dict):
            debug["helius_top_holders"] = f"bad response: {type(result)}"
            return
        accounts = result.get("value") or []
        debug["helius_top_holders_count"] = len(accounts)

        for acct in accounts[:30]:
            address = acct.get("address")
            if not address:
                continue
            amt_str = acct.get("amount")
            decimals = int(acct.get("decimals") or 0)
            try:
                balance = float(amt_str) / (10 ** decimals) if amt_str and decimals else float(amt_str or 0)
            except Exception:
                balance = 0.0

            acc_info = await helius.get_account_info(address)
            if not acc_info:
                continue
            try:
                owner = ((acc_info.get("data") or {}).get("parsed") or {}).get("info", {}).get("owner")
            except Exception:
                owner = None
            if not owner or owner in SKIP_ADDRESSES:
                continue

            value_usd = balance * price_usd if price_usd else 0.0
            w = _empty_wallet(owner)
            w["balance_tokens"] = balance
            w["balance_usd"] = round(value_usd, 2)
            w["unrealized_profit_usd"] = round(value_usd, 2)
            w["total_profit_usd"] = round(value_usd, 2)
            w["still_holding"] = True
            w["tags"] = ["helius_top_holder"]
            _merge_into(pool, owner, w, "helius_top_holder")
    except Exception as e:
        debug["helius_top_holders_error"] = str(e)[:200]


# ─── Source 4: Helius transaction-history parser ────────────────────────

async def _paginate_signatures(mint: str, max_sigs: int) -> list[dict]:
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
    events = []
    try:
        message = (tx.get("transaction") or {}).get("message") or {}
        meta = tx.get("meta") or {}
        account_keys = message.get("accountKeys") or []
        fee_payer = None
        if account_keys:
            first = account_keys[0]
            fee_payer = first.get("pubkey") if isinstance(first, dict) else str(first)

        pre = meta.get("preTokenBalances") or []
        post = meta.get("postTokenBalances") or []
        pre_by_key = {}
        for b in pre:
            if b.get("mint") != target_mint:
                continue
            o = b.get("owner")
            if o:
                pre_by_key[o] = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        post_by_key = {}
        for b in post:
            if b.get("mint") != target_mint:
                continue
            o = b.get("owner")
            if o:
                post_by_key[o] = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)

        all_owners = set(pre_by_key) | set(post_by_key)
        pre_sol = meta.get("preBalances") or []
        post_sol = meta.get("postBalances") or []
        sol_delta_lamports = 0
        if pre_sol and post_sol and len(pre_sol) > 0 and len(post_sol) > 0:
            sol_delta_lamports = post_sol[0] - pre_sol[0]
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
                "sol_amount": sol_amount if owner == fee_payer else 0.0,
            })
    except Exception:
        pass
    return events


async def _collect_helius_tx_history(mint: str, pool: dict, debug: dict):
    if not helius.configured:
        debug["helius_tx_history"] = "HELIUS_API_KEY not set"
        return
    try:
        price_usd = 0.0
        info = await geckoterminal.token_info("solana", mint)
        if info:
            attrs = info.get("attributes") or {}
            price_usd = float(attrs.get("price_usd") or 0)
        prices = await defillama.current_prices(["coingecko:solana"])
        sol_price_usd = float((prices.get("coingecko:solana") or {}).get("price") or 0)

        sigs = await _paginate_signatures(mint, DEFAULT_MAX_SIGNATURES)
        debug["helius_tx_signatures"] = len(sigs)
        if not sigs:
            return

        wallet_stats: dict[str, dict] = {}
        for sig_meta in sigs:
            sig = sig_meta.get("signature")
            if not sig:
                continue
            tx = await helius.get_transaction(sig)
            if not tx:
                continue
            for e in _parse_tx_for_swaps(tx, mint):
                w = e["wallet"]
                s = wallet_stats.setdefault(w, {
                    "tokens_bought": 0.0, "tokens_sold": 0.0,
                    "sol_spent": 0.0, "sol_received": 0.0, "tx_count": 0,
                })
                s["tx_count"] += 1
                if e["direction"] == "buy":
                    s["tokens_bought"] += e["token_amount"]
                    s["sol_spent"] += e["sol_amount"]
                else:
                    s["tokens_sold"] += e["token_amount"]
                    s["sol_received"] += e["sol_amount"]

        debug["helius_tx_wallets_found"] = len(wallet_stats)

        for addr, s in wallet_stats.items():
            current_balance = s["tokens_bought"] - s["tokens_sold"]
            current_value_usd = max(0, current_balance) * price_usd
            cost_usd = s["sol_spent"] * sol_price_usd
            received_usd = s["sol_received"] * sol_price_usd
            realized = 0.0
            if s["tokens_sold"] > 0 and s["tokens_bought"] > 0:
                avg_cost_per_token = cost_usd / s["tokens_bought"] if s["tokens_bought"] else 0
                realized = received_usd - (avg_cost_per_token * s["tokens_sold"])
            else:
                realized = received_usd - cost_usd
            unrealized = current_value_usd - (
                cost_usd * (max(0, current_balance) / s["tokens_bought"])
                if s["tokens_bought"] else 0
            )
            total_profit = realized + unrealized
            profit_mult = (total_profit / cost_usd) if cost_usd > 0 else 0.0

            w = _empty_wallet(addr)
            w["cost_usd"] = round(cost_usd, 2)
            w["realized_profit_usd"] = round(realized, 2)
            w["unrealized_profit_usd"] = round(unrealized, 2)
            w["total_profit_usd"] = round(total_profit, 2)
            w["profit_multiplier"] = round(profit_mult, 2)
            w["balance_tokens"] = round(max(0, current_balance), 6)
            w["balance_usd"] = round(current_value_usd, 2)
            w["tx_count"] = s["tx_count"]
            w["tokens_bought"] = round(s["tokens_bought"], 6)
            w["tokens_sold"] = round(s["tokens_sold"], 6)
            w["still_holding"] = current_balance > 0.0001
            w["fully_exited"] = s["tokens_sold"] > 0 and current_balance < 0.0001
            w["tags"] = ["helius_tx_history"]
            _merge_into(pool, addr, w, "helius_tx_history")
    except Exception as e:
        debug["helius_tx_history_error"] = str(e)[:200]


# ─── Public API ────────────────────────────────────────────────────────

async def extract_cabal_wallets(
    mint: str,
    *,
    min_profit_usd: float = DEFAULT_MIN_PROFIT_USD,
    min_profit_mult: float = DEFAULT_MIN_PROFIT_MULT,
    max_wallets: int = DEFAULT_MAX_WALLETS,
) -> tuple[list[dict], dict]:
    """Multi-source extraction. Merges GMGN + Helius top-holders + Helius tx history."""
    debug: dict = {"mint": mint}
    pool: dict[str, dict] = {}

    # Run all four collectors in sequence (they share rate-limit budgets)
    await _collect_from_gmgn(mint, pool, debug)
    await _collect_helius_top_holders(mint, pool, debug)
    await _collect_helius_tx_history(mint, pool, debug)

    debug["total_unique_wallets"] = len(pool)

    # Filter: keep if ANY of the following:
    #   - total_profit_usd >= min_profit_usd
    #   - balance_usd >= min_profit_usd (we value their current position)
    #   - appears in multiple sources (cross-confirmation)
    filtered = []
    for w in pool.values():
        cross_confirmed = len(w["sources"]) >= 2
        profit_ok = w["total_profit_usd"] >= min_profit_usd
        balance_ok = w["balance_usd"] >= min_profit_usd
        mult_ok = w["profit_multiplier"] >= min_profit_mult or min_profit_mult == 0
        if (profit_ok or balance_ok or cross_confirmed) and mult_ok:
            filtered.append(w)

    # Sort: cross-confirmed first, then by total_profit, then by balance
    def sort_key(w):
        return (
            -len(w["sources"]),
            -max(w["total_profit_usd"], w["balance_usd"]),
        )
    filtered.sort(key=sort_key)

    debug["post_filter_count"] = len(filtered)
    return filtered[:max_wallets], debug


def add_cabal_wallets_to_db(
    db: Session,
    wallets: list[dict],
    source_mint: str,
    source_symbol: str = "",
) -> int:
    """Insert extracted wallets. Returns count of new rows."""
    added = 0
    for w in wallets:
        addr = w["address"]
        existing = db.query(SolanaKnownWallet).filter_by(wallet_address=addr).first()
        if existing:
            if existing.role == "infrastructure":
                existing.role = "cabal_trader"
                existing.associated_mint = source_mint
            continue

        status_tag = (
            "exited" if w.get("fully_exited")
            else "holding" if w.get("still_holding")
            else "active"
        )
        profit_str = f"${w['total_profit_usd']:,.0f}" if w['total_profit_usd'] else f"${w['balance_usd']:,.0f} bal"
        srcs = "/".join(w.get("sources") or [])

        wallet = SolanaKnownWallet(
            wallet_address=addr,
            label=f"{source_symbol or 'runner'} — {profit_str} ({status_tag})",
            associated_token=source_symbol or None,
            associated_mint=source_mint,
            role="cabal_trader",
            is_active=True,
            total_profit_est=Decimal(str(round(
                max(w["total_profit_usd"], w["balance_usd"]), 2
            ))),
            added_at=datetime.utcnow(),
            notes=(
                f"From {source_symbol or source_mint[:10]}. "
                f"Realized ${w['realized_profit_usd']:,.0f}, "
                f"unrealized ${w['unrealized_profit_usd']:,.0f}, "
                f"balance ${w['balance_usd']:,.0f}. "
                f"Status: {status_tag}. "
                f"Sources: {srcs}."
            ),
        )
        db.add(wallet)
        added += 1

    db.commit()
    return added
