"""
Step 4: Exchange Flow Monitor — Exchange deposit/withdrawal detection.

Monitors watchlist token transfers for movements to/from known exchange wallets.
This is THE TRIGGER SIGNAL — insiders depositing to CEXs signals distribution.
Runs every 30 minutes for watchlist tokens.
"""

import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.bscscan_client import bscscan_request
from backend.models.flagged_token import FlaggedToken
from backend.models.exchange_wallet import ExchangeWallet
from backend.models.known_wallet import KnownWallet
from backend.models.wallet_activity import WalletActivity
from backend.models.watchlist import Watchlist

logger = logging.getLogger(__name__)


async def get_recent_token_transfers(contract_address: str) -> list[dict]:
    """Get the 100 most recent token transfers for a contract."""
    data = await bscscan_request({
        "module": "token",
        "action": "tokentx",
        "contractaddress": contract_address,
        "page": "1",
        "offset": "100",
        "sort": "desc",
    })
    if data and isinstance(data.get("result"), list):
        return data["result"]
    return []


def load_exchange_wallets(db: Session) -> dict[str, str]:
    """Load known exchange wallet addresses and their labels."""
    wallets = db.query(ExchangeWallet).all()
    return {w.wallet_address.lower(): w.exchange_name for w in wallets}


def load_known_wallets(db: Session) -> dict[str, str]:
    """Load known insider wallet addresses and labels."""
    wallets = db.query(KnownWallet).filter(KnownWallet.is_active.is_(True)).all()
    return {w.wallet_address.lower(): w.label for w in wallets}


def load_top_holders(db: Session, contract_address: str) -> set[str]:
    """Load top holder addresses for a token from watchlist analysis."""
    watchlist = db.query(Watchlist).filter_by(contract_address=contract_address).first()
    if watchlist and watchlist.cluster_funding_sources:
        return set(watchlist.cluster_funding_sources)
    return set()


async def analyze_exchange_flows(
    contract_address: str,
    db: Session,
    exchange_wallets: dict[str, str],
    known_wallets: dict[str, str],
) -> dict:
    """
    Analyze recent token transfers for exchange flow patterns.
    Returns flow analysis results.
    """
    result = {
        "deposits_detected": False,
        "deposit_count": 0,
        "deposit_volume": Decimal("0"),
        "deposit_details": [],
        "withdrawals_detected": False,
        "withdrawal_count": 0,
        "withdrawal_volume": Decimal("0"),
        "withdrawal_details": [],
        "insider_deposits": [],
    }

    transfers = await get_recent_token_transfers(contract_address)
    if not transfers:
        return result

    cluster_wallets = load_top_holders(db, contract_address)

    for tx in transfers:
        from_addr = tx.get("from", "").lower()
        to_addr = tx.get("to", "").lower()
        value_raw = tx.get("value", "0")
        decimals = int(tx.get("tokenDecimal", "18"))
        tx_hash = tx.get("hash", "")
        block = int(tx.get("blockNumber", 0))

        try:
            amount = Decimal(value_raw) / Decimal(10**decimals)
        except Exception:
            amount = Decimal("0")

        # Check for deposits TO exchange
        if to_addr in exchange_wallets:
            exchange_name = exchange_wallets[to_addr]
            result["deposits_detected"] = True
            result["deposit_count"] += 1
            result["deposit_volume"] += amount

            detail = {
                "from": from_addr,
                "exchange": exchange_name,
                "amount": str(amount),
                "tx_hash": tx_hash,
            }
            result["deposit_details"].append(detail)

            # Check if depositor is a known wallet or cluster member
            is_insider = (
                from_addr in known_wallets
                or from_addr in cluster_wallets
            )
            if is_insider:
                label = known_wallets.get(from_addr, "cluster member")
                result["insider_deposits"].append({
                    **detail,
                    "insider_label": label,
                })

                # Log activity
                activity = WalletActivity(
                    wallet_address=from_addr,
                    activity_type="exchange_deposit",
                    token_contract=contract_address,
                    token_symbol=tx.get("tokenSymbol", ""),
                    amount=amount,
                    counterparty=to_addr,
                    counterparty_label=exchange_name,
                    tx_hash=tx_hash,
                    block_number=block,
                    detected_at=datetime.utcnow(),
                    flagged=True,
                )
                # Only log if wallet exists in known_wallets table
                if from_addr in known_wallets:
                    db.add(activity)

        # Check for withdrawals FROM exchange (supply squeeze pattern)
        if from_addr in exchange_wallets:
            exchange_name = exchange_wallets[from_addr]
            result["withdrawals_detected"] = True
            result["withdrawal_count"] += 1
            result["withdrawal_volume"] += amount
            result["withdrawal_details"].append({
                "to": to_addr,
                "exchange": exchange_name,
                "amount": str(amount),
                "tx_hash": tx_hash,
            })

    db.commit()
    return result


def update_watchlist_with_flows(db: Session, contract_address: str, flows: dict):
    """Update watchlist entry with exchange flow data."""
    entry = db.query(Watchlist).filter_by(contract_address=contract_address).first()
    if entry:
        entry.exchange_deposits_detected = flows["deposits_detected"]
        entry.exchange_deposit_volume = flows["deposit_volume"]
        entry.last_scored_at = datetime.utcnow()
        db.commit()


async def run_exchange_flow_monitor():
    """Main exchange flow monitor entry point. Called every 30 minutes."""
    logger.info("Starting exchange flow monitor run...")
    db = SessionLocal()
    monitored_count = 0

    try:
        exchange_wallets = load_exchange_wallets(db)
        known_wallets = load_known_wallets(db)

        if not exchange_wallets:
            logger.warning("No exchange wallets in database. Seed exchange_wallets table first.")
            return

        # Monitor watchlist tokens
        watchlist_tokens = db.query(FlaggedToken).filter(
            FlaggedToken.status == "watchlist",
            FlaggedToken.removed.is_(False),
        ).all()

        logger.info(f"Monitoring exchange flows for {len(watchlist_tokens)} tokens")

        for token in watchlist_tokens:
            try:
                flows = await analyze_exchange_flows(
                    token.contract_address, db, exchange_wallets, known_wallets
                )
                update_watchlist_with_flows(db, token.contract_address, flows)
                monitored_count += 1

                if flows["insider_deposits"]:
                    logger.warning(
                        f"INSIDER DEPOSIT DETECTED: {token.token_symbol} — "
                        f"{len(flows['insider_deposits'])} deposits from known wallets"
                    )

            except Exception as e:
                logger.error(f"Error monitoring {token.contract_address}: {e}")
                continue

        logger.info(f"Exchange flow monitor complete. Monitored {monitored_count} tokens.")

    except Exception as e:
        logger.error(f"Exchange flow monitor error: {e}")
    finally:
        db.close()
