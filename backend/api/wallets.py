"""
Solana wallet API routes — cabal tracking.

Endpoints:
  POST /api/wallets/solana/extract-from-runner   — paste a runner CA,
        extract its early/profitable wallets, optionally add to DB
  GET  /api/wallets/solana/known                  — list tracked wallets
  GET  /api/wallets/solana/activity               — recent buy/sell feed
  GET  /api/wallets/solana/convergence            — mints that multiple
        tracked wallets bought in a recent window, scored by confidence
"""

import asyncio
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, desc
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models.solana_known_wallet import SolanaKnownWallet
from backend.models.solana_wallet_activity import SolanaWalletActivity

# Hard upper bound on a single extract request. Even with parallelized
# RPCs, a degraded Helius or a very deep history can stretch out — we'd
# rather the frontend get a clear error than a silent hang.
EXTRACT_TIMEOUT_SECONDS = 90

router = APIRouter(prefix="/api/wallets", tags=["wallets"])


class ExtractFromRunnerRequest(BaseModel):
    mint: str
    symbol: str = ""
    min_profit_usd: float = 500.0
    min_profit_mult: float = 0.0
    max_wallets: int = 60
    auto_add: bool = True


@router.post("/solana/extract-from-runner")
async def extract_from_runner(
    req: ExtractFromRunnerRequest,
    db: Session = Depends(get_db),
):
    """Paste a Solana runner CA → extract early profitable wallets."""
    from backend.detection.cabal_extractor import (
        extract_cabal_wallets,
        add_cabal_wallets_to_db,
    )

    try:
        wallets, debug = await asyncio.wait_for(
            extract_cabal_wallets(
                req.mint,
                min_profit_usd=req.min_profit_usd,
                min_profit_mult=req.min_profit_mult,
                max_wallets=req.max_wallets,
            ),
            timeout=EXTRACT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=(
                f"Extraction timed out after {EXTRACT_TIMEOUT_SECONDS}s. "
                "Likely a slow Helius response or rate limit. Try again."
            ),
        )

    added = 0
    if req.auto_add and wallets:
        added = add_cabal_wallets_to_db(db, wallets, req.mint, req.symbol)

    return {
        "mint": req.mint,
        "symbol": req.symbol,
        "wallets_found": len(wallets),
        "wallets_added": added,
        "wallets": [
            {
                "address": w["address"],
                "total_profit_usd": w["total_profit_usd"],
                "profit_multiplier": w["profit_multiplier"],
                "cost_usd": w["cost_usd"],
                "balance_usd": w["balance_usd"],
                "pct_of_supply": w["pct_of_supply"],
                "is_smart_money": w["is_smart_money"],
                "still_holding": w["still_holding"],
                "fully_exited": w["fully_exited"],
                "tags": w["tags"],
                "sources": w["sources"],
            }
            for w in wallets
        ],
        "debug": debug,
    }


def _short(addr: str | None) -> str | None:
    if not addr:
        return None
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 12 else addr


@router.get("/solana/known")
def get_solana_known_wallets(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    role: str | None = None,
    db: Session = Depends(get_db),
):
    """List tracked Solana wallets. Filter by role."""
    query = db.query(SolanaKnownWallet).filter(
        SolanaKnownWallet.is_active.is_(True)
    )
    if role:
        query = query.filter(SolanaKnownWallet.role == role)

    total = query.count()
    wallets = query.order_by(SolanaKnownWallet.added_at.desc()).offset(
        (page - 1) * per_page
    ).limit(per_page).all()

    return {
        "items": [
            {
                "wallet_address": w.wallet_address,
                "wallet_short": _short(w.wallet_address),
                "label": w.label,
                "role": w.role,
                "associated_token": w.associated_token,
                "associated_mint": w.associated_mint,
                "associated_mint_short": _short(w.associated_mint),
                "funding_source": w.funding_source,
                "funding_source_short": _short(w.funding_source),
                "total_profit_est": str(w.total_profit_est) if w.total_profit_est else None,
                "added_at": w.added_at.isoformat() if w.added_at else None,
                "notes": w.notes,
            }
            for w in wallets
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


# ─── Live activity ───────────────────────────────────────────────────

@router.get("/solana/activity")
def get_solana_activity(
    limit: int = Query(50, ge=1, le=200),
    since_minutes: int = Query(60 * 24, ge=1, le=60 * 24 * 7),
    only_buys: bool = True,
    db: Session = Depends(get_db),
):
    """Recent SPL activity by tracked wallets."""
    since = datetime.utcnow() - timedelta(minutes=since_minutes)
    query = (
        db.query(SolanaWalletActivity, SolanaKnownWallet)
        .outerjoin(
            SolanaKnownWallet,
            SolanaKnownWallet.wallet_address == SolanaWalletActivity.wallet_address,
        )
        .filter(SolanaWalletActivity.detected_at >= since)
    )
    if only_buys:
        query = query.filter(SolanaWalletActivity.activity_type == "spl_buy")

    rows = query.order_by(desc(SolanaWalletActivity.detected_at)).limit(limit).all()

    return {
        "since": since.isoformat(),
        "items": [
            {
                "wallet_address": a.wallet_address,
                "wallet_short": _short(a.wallet_address),
                "wallet_role": (w.role if w else None),
                "activity_type": a.activity_type,
                "token_mint": a.token_mint,
                "token_mint_short": _short(a.token_mint),
                "token_symbol": a.token_symbol,
                "amount": str(a.amount) if a.amount else None,
                "value_usd": str(a.value_usd) if a.value_usd else None,
                "signature": a.signature,
                "detected_at": a.detected_at.isoformat() if a.detected_at else None,
                "is_new_token": a.is_new_token,
            }
            for (a, w) in rows
        ],
    }


# Wallet role weights for the convergence confidence score.
_ROLE_WEIGHT = {
    "cabal_trader": 1.0,
    "cabal_linked": 0.7,
    "cabal_linked_2": 0.5,
}


@router.get("/solana/convergence")
def get_solana_convergence(
    window_minutes: int = Query(60, ge=5, le=60 * 24),
    min_wallets: int = Query(2, ge=2, le=20),
    db: Session = Depends(get_db),
):
    """Mints that multiple tracked wallets bought within the window,
    sorted by confidence. The score weights wallets by role so
    cabal_trader hits count more than graph-walk-discovered ones."""
    since = datetime.utcnow() - timedelta(minutes=window_minutes)

    # One row per (wallet, mint) — a wallet buying the same mint 3 times
    # in the window should count once.
    per_wallet = (
        db.query(
            SolanaWalletActivity.token_mint.label("mint"),
            SolanaWalletActivity.wallet_address.label("wallet"),
            func.min(SolanaWalletActivity.detected_at).label("first_buy"),
            func.max(SolanaWalletActivity.detected_at).label("last_buy"),
            func.sum(SolanaWalletActivity.value_usd).label("value_usd"),
            func.count(SolanaWalletActivity.id).label("tx_count"),
        )
        .filter(
            SolanaWalletActivity.detected_at >= since,
            SolanaWalletActivity.activity_type == "spl_buy",
            SolanaWalletActivity.token_mint.isnot(None),
        )
        .group_by(
            SolanaWalletActivity.token_mint,
            SolanaWalletActivity.wallet_address,
        )
        .subquery()
    )

    rows = (
        db.query(
            per_wallet.c.mint,
            per_wallet.c.wallet,
            per_wallet.c.first_buy,
            per_wallet.c.last_buy,
            per_wallet.c.value_usd,
            per_wallet.c.tx_count,
            SolanaKnownWallet.role,
        )
        .outerjoin(
            SolanaKnownWallet,
            SolanaKnownWallet.wallet_address == per_wallet.c.wallet,
        )
        .all()
    )

    # Group into {mint: {wallets: [...], first_buy, last_buy, score}}
    clusters: dict[str, dict] = {}
    for mint, wallet, first_buy, last_buy, value_usd, tx_count, role in rows:
        c = clusters.setdefault(mint, {
            "mint": mint,
            "mint_short": _short(mint),
            "wallets": [],
            "first_buy": first_buy,
            "last_buy": last_buy,
            "total_value_usd": 0.0,
            "score": 0.0,
        })
        weight = _ROLE_WEIGHT.get(role or "", 0.5)
        c["wallets"].append({
            "wallet_address": wallet,
            "wallet_short": _short(wallet),
            "role": role,
            "first_buy": first_buy.isoformat() if first_buy else None,
            "last_buy": last_buy.isoformat() if last_buy else None,
            "value_usd": float(value_usd) if value_usd else 0.0,
            "tx_count": tx_count,
            "weight": weight,
        })
        if first_buy and (c["first_buy"] is None or first_buy < c["first_buy"]):
            c["first_buy"] = first_buy
        if last_buy and (c["last_buy"] is None or last_buy > c["last_buy"]):
            c["last_buy"] = last_buy
        c["total_value_usd"] += float(value_usd) if value_usd else 0.0
        c["score"] += weight

    results = []
    for c in clusters.values():
        if len(c["wallets"]) < min_wallets:
            continue
        c["wallet_count"] = len(c["wallets"])
        c["first_buy"] = c["first_buy"].isoformat() if c["first_buy"] else None
        c["last_buy"] = c["last_buy"].isoformat() if c["last_buy"] else None
        c["total_value_usd"] = round(c["total_value_usd"], 2)
        c["score"] = round(c["score"], 2)
        results.append(c)

    results.sort(key=lambda r: (-r["score"], -r["wallet_count"]))

    return {
        "window_minutes": window_minutes,
        "min_wallets": min_wallets,
        "since": since.isoformat(),
        "clusters": results,
    }
