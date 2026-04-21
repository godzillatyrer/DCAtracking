"""
Cabal Wallet Extractor — paste a runner CA, get early profitable wallets.

Given a Solana token mint address of a recent "runner" (memecoin that
pumped hard), this module:
  1. Pulls top traders + top holders from GMGN
  2. Filters for wallets with early entry + high profit
  3. Optionally auto-adds them to `solana_known_wallets` as role='cabal_trader'
  4. Returns the ranked list so the user can review

These wallets then feed into:
  - Module 5 (insider_early_buyer) — alerts when ≥3 show up in a new launch
  - Module 3 (whale_fresh_wallet) — alerts when they fund fresh wallets
  - solana_graph_walk — follows their money to discover rotated wallets

Usage:
  POST /api/wallets/solana/extract-from-runner
  Body: {"mint": "Hon2rHAiqkcDtUzL5gA2vjXPr7T1MPCK2UT2AHKCpump"}
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from backend.clients import gmgn
from backend.config import settings
from backend.database import SessionLocal
from backend.models.solana_known_wallet import SolanaKnownWallet

logger = logging.getLogger(__name__)

# Default extraction filters
DEFAULT_MIN_PROFIT_USD = 5_000.0
DEFAULT_MIN_PROFIT_MULT = 3.0    # 3x minimum
DEFAULT_MAX_WALLETS = 30


async def _helius_top_holders(mint: str, limit: int = 20) -> tuple[list[dict], dict]:
    """
    Fallback when GMGN returns empty (Cloudflare blocks).

    Uses Solana RPC's getTokenLargestAccounts (top 20 by balance) +
    getAccountInfo per account to resolve owner wallets. This gives us
    CURRENT top holders but not historical PnL — we mark them as
    potential cabal members with profit=unknown and let the user
    verify on GMGN's web UI.

    Better than nothing: the top holders of a Pump.fun runner are very
    likely the cabal or at least connected to it.
    """
    from backend.clients import helius
    from backend.clients import geckoterminal

    debug: dict = {"label": "helius_fallback"}

    if not helius.configured:
        debug["error"] = "HELIUS_API_KEY not set"
        return [], debug

    # Step 1: getTokenLargestAccounts
    result = await helius._rpc("getTokenLargestAccounts", [mint])
    if not result or not isinstance(result, dict):
        debug["error"] = f"getTokenLargestAccounts returned: {type(result)}"
        return [], debug

    accounts = result.get("value") or []
    debug["token_accounts_found"] = len(accounts)

    # Step 2: get current price from GeckoTerminal for USD valuation
    price_usd = 0.0
    try:
        token_info = await geckoterminal.token_info("solana", mint)
        if token_info:
            attrs = token_info.get("attributes") or {}
            price_usd = float(attrs.get("price_usd") or 0)
    except Exception:
        pass
    debug["price_usd"] = price_usd

    # Step 3: resolve owner for each token account
    holders = []
    skip_addrs = {
        "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium auth
        "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM
        "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",   # Pump.fun
        "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM",    # Jupiter lock
    }
    for acct in accounts[:limit]:
        address = acct.get("address")
        if not address:
            continue
        # Amount is in raw units; need decimals
        amt_str = acct.get("amount")
        decimals = int(acct.get("decimals") or 0)
        try:
            balance = float(amt_str) / (10 ** decimals) if amt_str and decimals else float(amt_str or 0)
        except Exception:
            balance = 0.0

        # Resolve the wallet that OWNS this token account
        info = await helius.get_account_info(address)
        if not info:
            continue
        try:
            owner = ((info.get("data") or {}).get("parsed") or {}).get("info", {}).get("owner")
        except Exception:
            owner = None
        if not owner or owner in skip_addrs:
            continue

        value_usd = balance * price_usd if price_usd else 0.0

        holders.append({
            "address": owner,
            "realized_profit_usd": 0,  # unknown from on-chain snapshot
            "unrealized_profit_usd": round(value_usd, 2),
            "total_profit_usd": round(value_usd, 2),  # estimate: current holdings value
            "cost_usd": 0,  # unknown
            "profit_multiplier": 0,  # unknown
            "balance_tokens": balance,
            "balance_usd": round(value_usd, 2),
            "pct_of_supply": 0,
            "tags": ["helius_fallback"],
            "is_smart_money": False,
            "last_active": None,
            "raw": acct,
        })

    debug["holders_resolved"] = len(holders)
    return holders, debug


def _extract_field(item: dict, *candidates, default=None):
    """Try multiple possible field names; GMGN's response shape is
    undocumented so we adapt to whichever fields are present."""
    for key in candidates:
        val = item.get(key)
        if val is not None:
            return val
    return default


def _to_float(val, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _parse_trader(raw: dict) -> dict | None:
    """Normalize a single trader/holder entry from GMGN into a standard
    shape. Returns None if the entry lacks enough data to be useful."""
    addr = _extract_field(
        raw, "address", "wallet_address", "wallet", "holder_address",
    )
    if not addr or len(addr) < 20:
        return None

    realized = _to_float(_extract_field(
        raw, "realized_profit", "realized_pnl", "profit", "total_profit",
        "realized_profit_cur",
    ))
    unrealized = _to_float(_extract_field(
        raw, "unrealized_profit", "unrealized_pnl", "unrealized_profit_cur",
    ))
    total_profit = realized + unrealized

    cost = _to_float(_extract_field(
        raw, "cost", "total_cost", "buy_amount_cur", "buy_volume", "avg_cost",
    ))
    # Profit multiplier (X multiple on entry)
    if cost > 0:
        profit_mult = total_profit / cost
    else:
        profit_mult = 0.0

    balance = _to_float(_extract_field(
        raw, "balance", "amount", "token_balance", "amount_cur",
    ))
    balance_usd = _to_float(_extract_field(
        raw, "value", "usd_value", "balance_usd",
    ))
    pct_held = _to_float(_extract_field(
        raw, "percentage", "pct", "holder_percentage",
    ))

    tags = _extract_field(raw, "tags", "labels", "tag", default=[])
    if isinstance(tags, str):
        tags = [tags] if tags else []

    is_smart_money = _extract_field(raw, "is_smart_money", "smart_money", default=False)
    last_active = _extract_field(raw, "last_active_timestamp", "last_active", "timestamp")

    return {
        "address": addr,
        "realized_profit_usd": round(realized, 2),
        "unrealized_profit_usd": round(unrealized, 2),
        "total_profit_usd": round(total_profit, 2),
        "cost_usd": round(cost, 2),
        "profit_multiplier": round(profit_mult, 2),
        "balance_tokens": balance,
        "balance_usd": round(balance_usd, 2),
        "pct_of_supply": round(pct_held, 4),
        "tags": tags,
        "is_smart_money": bool(is_smart_money),
        "last_active": last_active,
        "raw": raw,
    }


def _extract_list_from_response(data, label: str = "") -> tuple[list[dict], dict]:
    """
    GMGN wraps responses in various shapes depending on the endpoint.
    This function aggressively unwraps until it finds a list of dicts.

    Returns (items, debug_info) where debug_info shows the response
    shape for troubleshooting field-name mismatches.
    """
    debug: dict = {"label": label, "type": type(data).__name__}

    if data is None:
        debug["note"] = "response was None"
        return [], debug

    if isinstance(data, list):
        debug["count"] = len(data)
        if data and isinstance(data[0], dict):
            debug["first_item_keys"] = list(data[0].keys())[:15]
        return [x for x in data if isinstance(x, dict)], debug

    if isinstance(data, dict):
        debug["top_keys"] = list(data.keys())[:20]
        # Try common wrapper keys
        for key in ("data", "items", "holders", "traders", "list",
                     "results", "records", "rows", "tokens"):
            val = data.get(key)
            if isinstance(val, list) and val:
                debug["unwrap_key"] = key
                debug["count"] = len(val)
                if isinstance(val[0], dict):
                    debug["first_item_keys"] = list(val[0].keys())[:15]
                return [x for x in val if isinstance(x, dict)], debug
        # Maybe the dict itself IS a single record? Or nested one more level.
        # Try all values that are lists.
        for k, v in data.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                debug["unwrap_key"] = k
                debug["count"] = len(v)
                debug["first_item_keys"] = list(v[0].keys())[:15]
                return v, debug
        debug["note"] = "dict had no list-valued key"
        return [], debug

    debug["note"] = f"unexpected type: {type(data)}"
    return [], debug


async def extract_cabal_wallets(
    mint: str,
    *,
    min_profit_usd: float = DEFAULT_MIN_PROFIT_USD,
    min_profit_mult: float = DEFAULT_MIN_PROFIT_MULT,
    max_wallets: int = DEFAULT_MAX_WALLETS,
) -> tuple[list[dict], dict]:
    """
    Pull top traders + holders for a token mint from GMGN, parse into a
    normalized list, and filter for likely cabal wallets.

    Returns (wallets, debug_info) where debug_info shows what GMGN
    returned at each step (for troubleshooting when wallets_found = 0).
    """
    all_raw: list[dict] = []
    debug_info: dict = {}

    # Pull from multiple GMGN endpoints — different data per endpoint.
    try:
        traders_raw = await gmgn.token_top_traders(mint, limit=100)
        items, d = _extract_list_from_response(traders_raw, "top_traders")
        all_raw.extend(items)
        debug_info["top_traders"] = d
    except Exception as e:
        debug_info["top_traders"] = {"error": str(e)[:200]}
        logger.warning(f"GMGN top_traders failed for {mint[:10]}: {e}")

    try:
        holders_raw = await gmgn.token_holders(mint, limit=50)
        items, d = _extract_list_from_response(holders_raw, "top_holders")
        all_raw.extend(items)
        debug_info["top_holders"] = d
    except Exception as e:
        debug_info["top_holders"] = {"error": str(e)[:200]}
        logger.warning(f"GMGN token_holders failed for {mint[:10]}: {e}")

    try:
        smart_raw = await gmgn.smart_money_trades(mint)
        items, d = _extract_list_from_response(smart_raw, "smart_money_trades")
        all_raw.extend(items)
        debug_info["smart_money_trades"] = d
    except Exception as e:
        debug_info["smart_money_trades"] = {"error": str(e)[:200]}
        logger.warning(f"GMGN smart_money_trades failed for {mint[:10]}: {e}")

    debug_info["total_raw_items_gmgn"] = len(all_raw)

    # Fallback: if GMGN returned nothing (Cloudflare block), use Helius
    # to get current top holders from on-chain data directly.
    if not all_raw:
        logger.info(f"GMGN empty for {mint[:10]} — falling back to Helius top holders")
        helius_holders, h_debug = await _helius_top_holders(mint, limit=max_wallets)
        debug_info["helius_fallback"] = h_debug
        # For Helius fallback, we skip the profit filter (we don't have
        # PnL data) and add ALL top holders. The user can review + prune.
        if helius_holders:
            all_raw.extend(helius_holders)
            # Return early with relaxed filters for Helius data
            seen: dict[str, dict] = {}
            for h in helius_holders:
                if h["address"] not in seen:
                    seen[h["address"]] = h
            result = sorted(seen.values(), key=lambda x: -x["balance_usd"])[:max_wallets]
            debug_info["total_raw_items"] = len(result)
            debug_info["source"] = "helius_fallback"
            debug_info["parsed_count"] = len(result)
            debug_info["post_filter_count"] = len(result)
            return result, debug_info

    debug_info["total_raw_items"] = len(all_raw)
    debug_info["source"] = "gmgn"
    logger.info(f"extract_cabal: {len(all_raw)} raw entries from GMGN for {mint[:10]}")

    # Parse + dedup
    seen: dict[str, dict] = {}
    for raw in all_raw:
        parsed = _parse_trader(raw)
        if not parsed:
            continue
        addr = parsed["address"]
        prev = seen.get(addr)
        # Keep the entry with more data (higher profit or more fields)
        if prev is None or parsed["total_profit_usd"] > prev["total_profit_usd"]:
            seen[addr] = parsed

    # Filter
    filtered = []
    for w in seen.values():
        if w["total_profit_usd"] < min_profit_usd:
            continue
        if w["profit_multiplier"] < min_profit_mult:
            continue
        filtered.append(w)

    # Sort by total profit descending
    filtered.sort(key=lambda x: -x["total_profit_usd"])

    # Cap
    debug_info["parsed_count"] = len(seen)
    debug_info["post_filter_count"] = len(filtered)
    return filtered[:max_wallets], debug_info


def add_cabal_wallets_to_db(
    db: Session,
    wallets: list[dict],
    source_mint: str,
    source_symbol: str = "",
) -> int:
    """
    Add extracted wallets to solana_known_wallets with role='cabal_trader'.
    Returns count of newly added wallets (skips duplicates).
    """
    added = 0
    for w in wallets:
        addr = w["address"]
        existing = db.query(SolanaKnownWallet).filter_by(wallet_address=addr).first()
        if existing:
            # Upgrade role if they were just 'infrastructure' before
            if existing.role == "infrastructure":
                existing.role = "cabal_trader"
                existing.associated_mint = source_mint
                existing.notes = (
                    f"Upgraded from infrastructure. Profit ${w['total_profit_usd']:,.0f} "
                    f"({w['profit_multiplier']:.1f}x) on {source_symbol or source_mint[:10]}."
                )
            continue

        label_parts = [f"${w['total_profit_usd']:,.0f} profit"]
        if w["profit_multiplier"] > 0:
            label_parts.append(f"{w['profit_multiplier']:.0f}x")
        if w.get("is_smart_money"):
            label_parts.append("smart money")
        if w.get("tags"):
            label_parts.extend(w["tags"][:2])

        wallet = SolanaKnownWallet(
            wallet_address=addr,
            label=f"{source_symbol or 'runner'} cabal — {', '.join(label_parts)}",
            associated_token=source_symbol or None,
            associated_mint=source_mint,
            role="cabal_trader",
            is_active=True,
            total_profit_est=Decimal(str(round(w["total_profit_usd"], 2))),
            added_at=datetime.utcnow(),
            notes=(
                f"Extracted from runner {source_symbol or source_mint[:10]}. "
                f"Cost ${w['cost_usd']:,.0f}, profit ${w['total_profit_usd']:,.0f} "
                f"({w['profit_multiplier']:.1f}x). "
                f"Tags: {w.get('tags')}. Smart money: {w.get('is_smart_money')}."
            ),
        )
        db.add(wallet)
        added += 1

    db.commit()
    return added
