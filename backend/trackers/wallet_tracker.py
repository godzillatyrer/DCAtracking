"""
Known Wallet Tracker — Monitor confirmed pump operators.

THE HIGHEST-ALPHA FEATURE.
Follows known insider wallets to detect their next play:
- New token accumulations
- Gas funding to fresh wallets (new cluster setup)
- Exchange interactions
- Inter-cluster transfers

Runs every 30-60 minutes.
"""

import asyncio
import logging
from datetime import datetime
from decimal import Decimal

import httpx
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import SessionLocal
from backend.models.flagged_token import FlaggedToken
from backend.models.known_wallet import KnownWallet
from backend.models.wallet_activity import WalletActivity
from backend.models.exchange_wallet import ExchangeWallet

logger = logging.getLogger(__name__)

BSCSCAN_BASE = settings.BSCSCAN_BASE_URL
API_KEY = settings.BSCSCAN_API_KEY
RATE_LIMIT_DELAY = 0.25


async def bscscan_request(params: dict) -> dict | None:
    """Make a rate-limited request to BscScan API."""
    params["apikey"] = API_KEY
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(BSCSCAN_BASE, params=params)
            await asyncio.sleep(RATE_LIMIT_DELAY)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "1":
                    return data
        except Exception as e:
            logger.error(f"BscScan API error: {e}")
    return None


async def get_recent_token_transfers(wallet_address: str) -> list[dict]:
    """Get the 50 most recent token transfers for a wallet."""
    data = await bscscan_request({
        "module": "account",
        "action": "tokentx",
        "address": wallet_address,
        "page": "1",
        "offset": "50",
        "sort": "desc",
    })
    if data and isinstance(data.get("result"), list):
        return data["result"]
    return []


async def get_recent_normal_transactions(wallet_address: str) -> list[dict]:
    """Get recent normal (BNB) transactions for gas funding detection."""
    data = await bscscan_request({
        "module": "account",
        "action": "txlist",
        "address": wallet_address,
        "page": "1",
        "offset": "20",
        "sort": "desc",
    })
    if data and isinstance(data.get("result"), list):
        return data["result"]
    return []


def get_known_token_holdings(db: Session, wallet_address: str) -> set[str]:
    """Get set of token contracts this wallet has previously interacted with."""
    activities = db.query(WalletActivity.token_contract).filter(
        WalletActivity.wallet_address == wallet_address,
        WalletActivity.token_contract.isnot(None),
    ).distinct().all()
    return {a[0].lower() for a in activities if a[0]}


async def track_wallet(
    wallet: KnownWallet,
    db: Session,
    exchange_addresses: set[str],
    known_addresses: set[str],
) -> list[dict]:
    """
    Track a single known wallet's recent activity.
    Returns list of notable activities detected.
    """
    notable = []
    wallet_addr = wallet.wallet_address.lower()
    known_holdings = get_known_token_holdings(db, wallet_addr)

    # 1. Check token transfers
    token_transfers = await get_recent_token_transfers(wallet_addr)
    for tx in token_transfers:
        from_addr = tx.get("from", "").lower()
        to_addr = tx.get("to", "").lower()
        token_contract = tx.get("contractAddress", "").lower()
        token_symbol = tx.get("tokenSymbol", "")
        value_raw = tx.get("value", "0")
        decimals = int(tx.get("tokenDecimal", "18"))
        tx_hash = tx.get("hash", "")
        block = int(tx.get("blockNumber", 0))

        try:
            amount = Decimal(value_raw) / Decimal(10**decimals)
        except Exception:
            amount = Decimal("0")

        # Skip if already logged
        existing = db.query(WalletActivity).filter_by(tx_hash=tx_hash).first()
        if existing:
            continue

        # Determine activity type
        activity_type = None
        is_new = False
        flagged = False
        counterparty = ""
        counterparty_label = ""

        if from_addr == wallet_addr:
            # Outgoing transfer
            if to_addr in exchange_addresses:
                activity_type = "exchange_deposit"
                counterparty = to_addr
                # Look up exchange name
                ex = db.query(ExchangeWallet).filter_by(wallet_address=to_addr).first()
                counterparty_label = ex.exchange_name if ex else "Unknown Exchange"
                flagged = True
            elif to_addr in known_addresses:
                activity_type = "transfer_out"
                counterparty = to_addr
                counterparty_label = "Known wallet"
            else:
                activity_type = "sell" if to_addr != wallet_addr else "transfer_out"
                counterparty = to_addr

        elif to_addr == wallet_addr:
            # Incoming transfer
            if from_addr in exchange_addresses:
                activity_type = "exchange_withdrawal"
                counterparty = from_addr
                ex = db.query(ExchangeWallet).filter_by(wallet_address=from_addr).first()
                counterparty_label = ex.exchange_name if ex else "Unknown Exchange"
                flagged = True
            else:
                activity_type = "buy"
                counterparty = from_addr

            # Check if this is a NEW token for this wallet
            if token_contract not in known_holdings:
                is_new = True
                activity_type = "new_token_accumulation"
                flagged = True

        if not activity_type:
            continue

        # Log the activity
        activity = WalletActivity(
            wallet_address=wallet_addr,
            activity_type=activity_type,
            token_contract=token_contract,
            token_symbol=token_symbol,
            amount=amount,
            counterparty=counterparty,
            counterparty_label=counterparty_label,
            tx_hash=tx_hash,
            block_number=block,
            detected_at=datetime.utcnow(),
            is_new_token=is_new,
            flagged=flagged,
        )
        db.add(activity)

        if flagged:
            notable.append({
                "wallet": wallet_addr,
                "wallet_label": wallet.label,
                "activity": activity_type,
                "token_contract": token_contract,
                "token_symbol": token_symbol,
                "amount": str(amount),
                "is_new_token": is_new,
            })

    # 2. Check normal transactions for gas funding to new wallets
    normal_txs = await get_recent_normal_transactions(wallet_addr)
    for tx in normal_txs:
        from_addr = tx.get("from", "").lower()
        to_addr = tx.get("to", "").lower()
        value = Decimal(tx.get("value", "0")) / Decimal(10**18)  # BNB has 18 decimals
        tx_hash = tx.get("hash", "")
        block = int(tx.get("blockNumber", 0))

        if from_addr != wallet_addr:
            continue
        if value < Decimal("0.001"):
            continue  # Skip dust

        # Skip if already logged
        existing = db.query(WalletActivity).filter_by(tx_hash=tx_hash).first()
        if existing:
            continue

        # Small BNB transfer to a new address = possible gas funding for cluster
        if value < Decimal("1") and to_addr not in known_addresses and to_addr not in exchange_addresses:
            activity = WalletActivity(
                wallet_address=wallet_addr,
                activity_type="gas_funding",
                token_contract=None,
                token_symbol="BNB",
                amount=value,
                counterparty=to_addr,
                counterparty_label="New wallet (potential cluster)",
                tx_hash=tx_hash,
                block_number=block,
                detected_at=datetime.utcnow(),
                is_new_token=False,
                flagged=True,
            )
            db.add(activity)
            notable.append({
                "wallet": wallet_addr,
                "wallet_label": wallet.label,
                "activity": "gas_funding",
                "token_contract": None,
                "token_symbol": "BNB",
                "amount": str(value),
                "funded_wallet": to_addr,
            })

    db.commit()
    return notable


def auto_flag_new_token(db: Session, token_contract: str, detected_via: str):
    """
    Automatically flag a token discovered through wallet tracking.
    If a known operator is accumulating, this is a high-priority signal.
    """
    existing = db.query(FlaggedToken).filter_by(contract_address=token_contract).first()
    if not existing:
        flagged = FlaggedToken(
            contract_address=token_contract,
            chain="bsc",
            first_flagged_at=datetime.utcnow(),
            last_seen_at=datetime.utcnow(),
            status="candidate",  # Skip raw, go straight to candidate
        )
        db.add(flagged)
        db.commit()
        logger.warning(f"Auto-flagged new token {token_contract} via {detected_via}")


async def run_wallet_tracker():
    """Main wallet tracker entry point. Called every 30-60 minutes."""
    logger.info("Starting wallet tracker run...")
    db = SessionLocal()
    tracked_count = 0
    all_notable = []

    try:
        # Load reference data
        active_wallets = db.query(KnownWallet).filter(
            KnownWallet.is_active.is_(True)
        ).all()
        exchange_addresses = {
            w.wallet_address.lower()
            for w in db.query(ExchangeWallet).all()
        }
        known_addresses = {
            w.wallet_address.lower()
            for w in active_wallets
        }

        logger.info(f"Tracking {len(active_wallets)} known wallets")

        for wallet in active_wallets:
            try:
                notable = await track_wallet(wallet, db, exchange_addresses, known_addresses)
                tracked_count += 1
                all_notable.extend(notable)

                # Auto-flag tokens from new accumulations
                for item in notable:
                    if item["activity"] == "new_token_accumulation" and item.get("token_contract"):
                        auto_flag_new_token(
                            db,
                            item["token_contract"],
                            f"known operator {item['wallet_label']}",
                        )

            except Exception as e:
                logger.error(f"Error tracking wallet {wallet.wallet_address}: {e}")
                continue

        logger.info(
            f"Wallet tracker complete. Tracked {tracked_count} wallets, "
            f"{len(all_notable)} notable activities."
        )

        for item in all_notable:
            logger.warning(
                f"NOTABLE: {item['wallet_label']} — {item['activity']} — "
                f"{item.get('token_symbol', 'BNB')} ({item.get('amount', '')})"
            )

        return all_notable

    except Exception as e:
        logger.error(f"Wallet tracker error: {e}")
        return []
    finally:
        db.close()
