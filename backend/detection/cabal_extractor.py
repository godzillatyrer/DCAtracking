"""
Cabal Wallet Extractor — multi-source.

Given a Solana runner CA, pull early buyers + sellers + top holders
from every data source we have, merge + dedup by wallet, then insert
into solana_known_wallets.

Sources (merged, richest-data-wins on dup):
  1. GMGN /tokens/top_holders/sol/{mint}   — current top holders w/ balance
  2. GMGN /tokens/top_traders/sol/{mint}   — top traders ranked by PnL
  3. Helius getTokenLargestAccounts + account-info resolution
  4. Helius transaction-history parse — captures sellers who exited

Filter (for the user's goal: early buyers still holding or exited at
profit), we keep any wallet that:
  - has tokens_bought > 0, OR
  - appears in >= 2 sources (cross-confirmation), OR
  - has realized_profit_usd >= min_profit_usd, OR
  - has balance_usd >= min_profit_usd

Wallets are sorted by max(balance_usd, realized_profit_usd), cross-
confirmed first. Rank is what matters — strict filters mean zero hits
for brand-new tokens with thin external data.
"""

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy.orm import Session

from backend.clients import birdeye, defillama, geckoterminal, gmgn, helius
from backend.config import settings
from backend.database import SessionLocal
from backend.models.solana_known_wallet import SolanaKnownWallet

logger = logging.getLogger(__name__)

DEFAULT_MIN_PROFIT_USD = 500.0
DEFAULT_MIN_PROFIT_MULT = 0.0
DEFAULT_MAX_WALLETS = 60
DEFAULT_MAX_SIGNATURES = 600  # balanced: catches most early buyers without
                              # blowing past Helius free-tier rate limits
HELIUS_CONCURRENCY = 10       # parallel getTransaction calls; free tier is
                              # ~10 req/sec, stay under that
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
        traders = await gmgn.token_top_traders(mint, limit=100)
        items = _unwrap(traders)
        debug["gmgn_top_traders_count"] = len(items)
        if not items and traders is not None:
            debug["gmgn_top_traders_raw"] = str(traders)[:200]
        for raw in items:
            if not isinstance(raw, dict):
                continue
            addr, w = _extract_gmgn_wallet(raw)
            if addr:
                _merge_into(pool, addr, w, "gmgn_trader")
    except Exception as e:
        debug["gmgn_top_traders_error"] = str(e)[:200]

    try:
        holders = await gmgn.token_holders(mint, limit=100)
        items = _unwrap(holders)
        debug["gmgn_top_holders_count"] = len(items)
        if not items and holders is not None:
            debug["gmgn_top_holders_raw"] = str(holders)[:200]
        for raw in items:
            if not isinstance(raw, dict):
                continue
            addr, w = _extract_gmgn_wallet(raw)
            if addr:
                _merge_into(pool, addr, w, "gmgn_holder")
    except Exception as e:
        debug["gmgn_top_holders_error"] = str(e)[:200]


async def _resolve_token_price_usd(mint: str) -> tuple[float, str]:
    """Price fallback chain: GeckoTerminal → DexScreener → Birdeye.
    Returns (price_usd, source). 0.0 if nothing succeeded."""
    try:
        info = await geckoterminal.token_info("solana", mint)
        if info:
            price = float((info.get("attributes") or {}).get("price_usd") or 0)
            if price > 0:
                return price, "geckoterminal"
    except Exception:
        pass

    try:
        url = f"{settings.DEXSCREENER_BASE_URL}/latest/dex/tokens/{mint}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                pairs = (resp.json() or {}).get("pairs") or []
                # Take max liquidity pair's price
                best = None
                best_liq = 0
                for p in pairs:
                    liq = float(((p.get("liquidity") or {}).get("usd")) or 0)
                    if liq >= best_liq:
                        best_liq = liq
                        best = p
                if best:
                    price = float(best.get("priceUsd") or 0)
                    if price > 0:
                        return price, "dexscreener"
    except Exception:
        pass

    if birdeye.configured:
        try:
            overview = await birdeye.token_overview(mint)
            if overview:
                price = float(overview.get("price") or 0)
                if price > 0:
                    return price, "birdeye"
        except Exception:
            pass

    return 0.0, "none"


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

async def _collect_helius_top_holders(
    mint: str, pool: dict, debug: dict, price_usd: float,
):
    if not helius.configured:
        debug["helius_top_holders"] = "HELIUS_API_KEY not set"
        return
    try:
        result = await helius._rpc("getTokenLargestAccounts", [mint])
        if not result or not isinstance(result, dict):
            debug["helius_top_holders"] = f"bad response: {type(result)}"
            return
        accounts = (result.get("value") or [])[:30]
        debug["helius_top_holders_count"] = len(accounts)

        # Parallelize the per-account getAccountInfo calls — 30 sequential
        # RPCs at ~250ms each = 7s. With HELIUS_CONCURRENCY it's <1s.
        sem = asyncio.Semaphore(HELIUS_CONCURRENCY)

        async def _resolve(addr: str):
            async with sem:
                return await helius.get_account_info(addr)

        addrs = [a.get("address") for a in accounts if a.get("address")]
        infos = await asyncio.gather(
            *[_resolve(a) for a in addrs], return_exceptions=True
        )
        info_by_addr = {
            a: i for a, i in zip(addrs, infos)
            if i and not isinstance(i, Exception)
        }

        for acct in accounts:
            address = acct.get("address")
            if not address:
                continue
            amt_str = acct.get("amount")
            decimals = int(acct.get("decimals") or 0)
            try:
                balance = float(amt_str) / (10 ** decimals) if amt_str and decimals else float(amt_str or 0)
            except Exception:
                balance = 0.0

            acc_info = info_by_addr.get(address)
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
    """Extract (wallet, direction, token_amount, sol_amount) per wallet
    whose balance of `target_mint` changed. SOL amount is attributed
    using each wallet's own index in accountKeys — so bot-routed swaps
    where fee_payer differs from the trader still get correct credit
    whenever the trader signs the tx (very common on pump.fun).
    """
    events = []
    try:
        message = (tx.get("transaction") or {}).get("message") or {}
        meta = tx.get("meta") or {}
        account_keys = message.get("accountKeys") or []

        # pubkey -> index in accountKeys (for SOL delta lookup)
        key_index: dict[str, int] = {}
        for i, k in enumerate(account_keys):
            pubkey = k.get("pubkey") if isinstance(k, dict) else str(k)
            if pubkey:
                key_index[pubkey] = i

        fee_payer = None
        if account_keys:
            first = account_keys[0]
            fee_payer = first.get("pubkey") if isinstance(first, dict) else str(first)

        pre = meta.get("preTokenBalances") or []
        post = meta.get("postTokenBalances") or []
        pre_by_owner: dict[str, float] = {}
        for b in pre:
            if b.get("mint") != target_mint:
                continue
            o = b.get("owner")
            if o:
                pre_by_owner[o] = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        post_by_owner: dict[str, float] = {}
        for b in post:
            if b.get("mint") != target_mint:
                continue
            o = b.get("owner")
            if o:
                post_by_owner[o] = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)

        all_owners = set(pre_by_owner) | set(post_by_owner)
        pre_sol = meta.get("preBalances") or []
        post_sol = meta.get("postBalances") or []

        for owner in all_owners:
            if owner in SKIP_ADDRESSES:
                continue
            pre_amt = pre_by_owner.get(owner, 0)
            post_amt = post_by_owner.get(owner, 0)
            token_delta = post_amt - pre_amt
            if abs(token_delta) < 1e-9:
                continue

            # Per-owner SOL delta from accountKeys index
            sol_amount = 0.0
            idx = key_index.get(owner)
            if idx is not None and idx < len(pre_sol) and idx < len(post_sol):
                sol_amount = abs(post_sol[idx] - pre_sol[idx]) / LAMPORTS_PER_SOL
            elif owner == fee_payer and pre_sol and post_sol:
                # Fallback: fee_payer is always index 0
                sol_amount = abs(post_sol[0] - pre_sol[0]) / LAMPORTS_PER_SOL

            direction = "buy" if token_delta > 0 else "sell"
            events.append({
                "wallet": owner,
                "direction": direction,
                "token_amount": abs(token_delta),
                "sol_amount": sol_amount,
            })
    except Exception:
        pass
    return events


async def _collect_helius_tx_history(
    mint: str, pool: dict, debug: dict, price_usd: float, max_signatures: int,
):
    if not helius.configured:
        debug["helius_tx_history"] = "HELIUS_API_KEY not set"
        return
    try:
        prices = await defillama.current_prices(["coingecko:solana"])
        sol_price_usd = float((prices.get("coingecko:solana") or {}).get("price") or 0)

        sigs = await _paginate_signatures(mint, max_signatures)
        debug["helius_tx_signatures"] = len(sigs)
        if not sigs:
            return

        # Fetch tx bodies in bounded-concurrency batches. Sequential
        # with 50ms sleep/call is ~250s for 600 sigs — the browser
        # times out. At 10 concurrent we're ~25s, well under the
        # Helius free-tier 10 req/sec budget.
        sem = asyncio.Semaphore(HELIUS_CONCURRENCY)

        async def _fetch(sig: str) -> dict | None:
            async with sem:
                return await helius.get_transaction(sig)

        sig_ids = [s.get("signature") for s in sigs if s.get("signature")]
        tx_results = await asyncio.gather(
            *[_fetch(s) for s in sig_ids], return_exceptions=True
        )

        wallet_stats: dict[str, dict] = {}
        for tx in tx_results:
            if not tx or isinstance(tx, Exception):
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
            current_balance = max(0.0, s["tokens_bought"] - s["tokens_sold"])
            current_value_usd = current_balance * price_usd
            cost_usd = s["sol_spent"] * sol_price_usd
            received_usd = s["sol_received"] * sol_price_usd

            # Cost basis is per-token, so realized/unrealized split it
            # proportionally — no double counting of cost_usd.
            if s["tokens_bought"] > 0:
                avg_cost_per_token = cost_usd / s["tokens_bought"]
                cost_of_sold = avg_cost_per_token * s["tokens_sold"]
                cost_of_held = avg_cost_per_token * current_balance
            else:
                # Tokens appeared from airdrop / transfer; no basis to apply
                avg_cost_per_token = 0.0
                cost_of_sold = 0.0
                cost_of_held = 0.0

            realized = received_usd - cost_of_sold
            unrealized = current_value_usd - cost_of_held
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
    max_signatures: int = DEFAULT_MAX_SIGNATURES,
) -> tuple[list[dict], dict]:
    """Multi-source extraction. Merges GMGN + Helius top-holders + Helius tx history."""
    debug: dict = {"mint": mint}
    pool: dict[str, dict] = {}

    # Price resolution: GeckoTerminal → DexScreener → Birdeye. Zero if
    # all three fail (common on brand-new pump.fun tokens).
    price_usd, price_source = await _resolve_token_price_usd(mint)
    debug["token_price_usd"] = price_usd
    debug["token_price_source"] = price_source

    await _collect_from_gmgn(mint, pool, debug)
    await _collect_helius_top_holders(mint, pool, debug, price_usd)
    await _collect_helius_tx_history(mint, pool, debug, price_usd, max_signatures)

    debug["total_unique_wallets"] = len(pool)

    # Filter for user's goal: "early buyers who bought and are either
    # still holding or exited profitably". Keep any wallet that either:
    #   - actually bought the token (tokens_bought > 0), OR
    #   - appears in >= 2 sources (cross-confirmed), OR
    #   - has realized profit or current balance above the threshold
    # Threshold gates are OR'd so a single good signal is enough — we
    # rank the winners below, we don't rely on strict filters.
    filtered = []
    for w in pool.values():
        bought = (w.get("tokens_bought") or 0) > 0
        cross_confirmed = len(w["sources"]) >= 2
        profit_ok = w["realized_profit_usd"] >= min_profit_usd
        balance_ok = w["balance_usd"] >= min_profit_usd
        mult_ok = min_profit_mult == 0 or w["profit_multiplier"] >= min_profit_mult
        if (bought or cross_confirmed or profit_ok or balance_ok) and mult_ok:
            filtered.append(w)

    # Rank: cross-confirmed wallets first, then by the larger of realized
    # profit or current holding value. Gives priority to confirmed cabal
    # signal over raw PnL noise.
    def sort_key(w):
        return (
            -len(w["sources"]),
            -max(
                w["realized_profit_usd"],
                w["balance_usd"],
                w["total_profit_usd"],
            ),
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
