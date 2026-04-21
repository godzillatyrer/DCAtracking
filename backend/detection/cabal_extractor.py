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


async def extract_cabal_wallets(
    mint: str,
    *,
    min_profit_usd: float = DEFAULT_MIN_PROFIT_USD,
    min_profit_mult: float = DEFAULT_MIN_PROFIT_MULT,
    max_wallets: int = DEFAULT_MAX_WALLETS,
) -> list[dict]:
    """
    Pull top traders + holders for a token mint from GMGN, parse into a
    normalized list, and filter for likely cabal wallets.

    Returns a list of dicts sorted by total_profit_usd descending.
    """
    all_raw: list[dict] = []

    # Pull from multiple GMGN endpoints — different data per endpoint.
    # top_traders has PnL; top_holders has current balance + % supply.
    # We merge both and dedup by address.
    try:
        traders = await gmgn.token_top_traders(mint, limit=100)
        if isinstance(traders, list):
            all_raw.extend(traders)
        elif isinstance(traders, dict):
            all_raw.extend(traders.get("data") or traders.get("items") or [])
    except Exception as e:
        logger.warning(f"GMGN top_traders failed for {mint[:10]}: {e}")

    try:
        holders = await gmgn.token_holders(mint, limit=50)
        if isinstance(holders, list):
            all_raw.extend(holders)
        elif isinstance(holders, dict):
            all_raw.extend(holders.get("data") or holders.get("items") or [])
    except Exception as e:
        logger.warning(f"GMGN token_holders failed for {mint[:10]}: {e}")

    try:
        smart = await gmgn.smart_money_trades(mint)
        if isinstance(smart, list):
            all_raw.extend(smart)
        elif isinstance(smart, dict):
            all_raw.extend(smart.get("data") or smart.get("items") or [])
    except Exception as e:
        logger.warning(f"GMGN smart_money_trades failed for {mint[:10]}: {e}")

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
    return filtered[:max_wallets]


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
