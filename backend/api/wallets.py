"""
Wallet Tracker API routes — known operator wallets and activity.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, distinct
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models.known_wallet import KnownWallet
from backend.models.wallet_activity import WalletActivity

router = APIRouter(prefix="/api/wallets", tags=["wallets"])


class AddWalletRequest(BaseModel):
    wallet_address: str
    label: str = ""
    associated_token: str = ""
    associated_contract: str = ""
    role: str = "accumulator"
    notes: str = ""


@router.get("/known")
def get_known_wallets(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    active_only: bool = True,
    db: Session = Depends(get_db),
):
    """All known operator wallets with token holding counts."""
    query = db.query(KnownWallet)
    if active_only:
        query = query.filter(KnownWallet.is_active.is_(True))

    total = query.count()
    wallets = query.order_by(KnownWallet.added_at.desc()).offset(
        (page - 1) * per_page
    ).limit(per_page).all()

    items = []
    for w in wallets:
        # Count distinct tokens this wallet has interacted with
        token_count = db.query(func.count(distinct(WalletActivity.token_contract))).filter(
            WalletActivity.wallet_address == w.wallet_address,
            WalletActivity.token_contract.isnot(None),
        ).scalar() or 0

        items.append({
            "wallet_address": w.wallet_address,
            "label": w.label,
            "associated_token": w.associated_token,
            "associated_contract": w.associated_contract,
            "role": w.role,
            "funding_source": w.funding_source,
            "is_active": w.is_active,
            "total_profit_est": str(w.total_profit_est) if w.total_profit_est else None,
            "first_seen_at": w.first_seen_at.isoformat() if w.first_seen_at else None,
            "added_at": w.added_at.isoformat() if w.added_at else None,
            "notes": w.notes,
            "token_count": token_count,
        })

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/{address}/activity")
def get_wallet_activity(
    address: str,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    flagged_only: bool = False,
    db: Session = Depends(get_db),
):
    """Activity feed for a specific wallet."""
    address = address.lower()
    query = db.query(WalletActivity).filter_by(wallet_address=address)

    if flagged_only:
        query = query.filter(WalletActivity.flagged.is_(True))

    total = query.count()
    activities = query.order_by(WalletActivity.detected_at.desc()).offset(
        (page - 1) * per_page
    ).limit(per_page).all()

    return {
        "items": [
            {
                "id": a.id,
                "wallet_address": a.wallet_address,
                "activity_type": a.activity_type,
                "token_contract": a.token_contract,
                "token_symbol": a.token_symbol,
                "amount": str(a.amount) if a.amount else None,
                "value_usd": str(a.value_usd) if a.value_usd else None,
                "counterparty": a.counterparty,
                "counterparty_label": a.counterparty_label,
                "tx_hash": a.tx_hash,
                "detected_at": a.detected_at.isoformat() if a.detected_at else None,
                "is_new_token": a.is_new_token,
                "flagged": a.flagged,
            }
            for a in activities
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/active-positions")
def get_active_positions(db: Session = Depends(get_db)):
    """What tokens are known operators currently holding (recent accumulations)."""
    # Get distinct token contracts from recent buy/accumulation activities
    recent_accumulations = db.query(
        WalletActivity.token_contract,
        WalletActivity.token_symbol,
        WalletActivity.wallet_address,
    ).filter(
        WalletActivity.activity_type.in_(["buy", "new_token_accumulation"]),
        WalletActivity.token_contract.isnot(None),
    ).order_by(WalletActivity.detected_at.desc()).limit(100).all()

    # Group by token
    positions: dict[str, dict] = {}
    for contract, symbol, wallet in recent_accumulations:
        if contract not in positions:
            positions[contract] = {
                "token_contract": contract,
                "token_symbol": symbol,
                "wallet_count": 0,
                "wallets": [],
            }
        if wallet not in positions[contract]["wallets"]:
            positions[contract]["wallets"].append(wallet)
            positions[contract]["wallet_count"] += 1

    return list(positions.values())


@router.get("/new-accumulations")
def get_new_accumulations(
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """NEW token purchases by known operators (high priority)."""
    activities = db.query(WalletActivity).filter(
        WalletActivity.is_new_token.is_(True),
    ).order_by(WalletActivity.detected_at.desc()).limit(limit).all()

    results = []
    for a in activities:
        wallet = db.query(KnownWallet).filter_by(wallet_address=a.wallet_address).first()
        results.append({
            "wallet_address": a.wallet_address,
            "wallet_label": wallet.label if wallet else "Unknown",
            "wallet_role": wallet.role if wallet else None,
            "associated_token": wallet.associated_token if wallet else None,
            "token_contract": a.token_contract,
            "token_symbol": a.token_symbol,
            "amount": str(a.amount) if a.amount else None,
            "detected_at": a.detected_at.isoformat() if a.detected_at else None,
            "tx_hash": a.tx_hash,
        })

    return results


@router.post("/add")
def add_wallet(request: AddWalletRequest, db: Session = Depends(get_db)):
    """Manually add a wallet to track."""
    address = request.wallet_address.lower()

    existing = db.query(KnownWallet).filter_by(wallet_address=address).first()
    if existing:
        raise HTTPException(status_code=409, detail="Wallet already tracked")

    wallet = KnownWallet(
        wallet_address=address,
        label=request.label,
        associated_token=request.associated_token,
        associated_contract=request.associated_contract,
        role=request.role,
        is_active=True,
        notes=request.notes,
    )
    db.add(wallet)
    db.commit()

    return {"status": "added", "wallet_address": address}


@router.get("/balances")
async def get_wallet_balances(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Get BNB balances for known wallets (current page)."""
    from backend.bscscan_client import _rpc_call, _hex_to_int

    wallets = db.query(KnownWallet).filter(
        KnownWallet.is_active.is_(True)
    ).order_by(KnownWallet.added_at.desc()).offset(
        (page - 1) * per_page
    ).limit(per_page).all()

    results = {}
    for w in wallets:
        try:
            balance_hex = await _rpc_call("eth_getBalance", [w.wallet_address, "latest"])
            if balance_hex:
                balance_wei = _hex_to_int(balance_hex)
                balance_bnb = balance_wei / 1e18
                results[w.wallet_address] = round(balance_bnb, 4)
            else:
                results[w.wallet_address] = None
        except Exception:
            results[w.wallet_address] = None

    return results
