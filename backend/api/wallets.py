"""
Solana wallet API routes — cabal tracking.

Endpoints:
  POST /api/wallets/solana/extract-from-runner   — paste a runner CA,
        extract its early/profitable wallets, optionally add to DB
  GET  /api/wallets/solana/known                  — list tracked wallets
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models.solana_known_wallet import SolanaKnownWallet

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
    """
    Paste a Solana runner CA → extract early profitable wallets from
    every data source we have (GMGN traders/holders + Helius top holders
    + Helius tx history) → optionally auto-add to solana_known_wallets
    as role='cabal_trader'.
    """
    from backend.detection.cabal_extractor import (
        extract_cabal_wallets,
        add_cabal_wallets_to_db,
    )

    wallets, debug = await extract_cabal_wallets(
        req.mint,
        min_profit_usd=req.min_profit_usd,
        min_profit_mult=req.min_profit_mult,
        max_wallets=req.max_wallets,
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
                "label": w.label,
                "role": w.role,
                "associated_token": w.associated_token,
                "associated_mint": w.associated_mint,
                "funding_source": w.funding_source,
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
