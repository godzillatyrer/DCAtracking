"""
Wallet Seeder — Seed known wallets from confirmed pump tokens.

Extracts top holders, deployers, and cluster patterns from the 5 confirmed
pump tokens and populates the known_wallets table.
"""

import asyncio
import logging
from datetime import datetime
from collections import defaultdict

import httpx
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import SessionLocal
from backend.models.known_wallet import KnownWallet

logger = logging.getLogger(__name__)

BSCSCAN_BASE = settings.BSCSCAN_BASE_URL
API_KEY = settings.BSCSCAN_API_KEY
RATE_LIMIT_DELAY = 0.25

# Confirmed pump tokens with contract addresses
CONFIRMED_PUMPS = [
    {
        "symbol": "RAVE",
        "name": "RaveDAO",
        "contract": "0x17205fab260a7a6383a81452ce6315a39370db97",
    },
    {
        "symbol": "SIREN",
        "name": "SirenCoin",
        "contract": "0x997a58129890bbda032231a52ed1ddc845fc18e1",
    },
    {
        "symbol": "RIVER",
        "name": "River Protocol",
        "contract": "0xda7ad9dea9397cffddae2f8a052b82f1484252b3",
    },
    {
        "symbol": "ARIA",
        "name": "AriaAI",
        "contract": "0x5d3a12c42e5372b2cc3264ab3cdcf660a1555238",
    },
    {
        "symbol": "STO",
        "name": "StakeStone",
        "contract": "",  # To be looked up on BscScan
    },
]


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


async def get_deployer(contract_address: str) -> str | None:
    """Get contract deployer/creator address."""
    data = await bscscan_request({
        "module": "contract",
        "action": "getcontractcreation",
        "contractaddresses": contract_address,
    })
    if data and isinstance(data.get("result"), list) and len(data["result"]) > 0:
        return data["result"][0].get("contractCreator", "").lower()
    return None


async def get_top_holders(contract_address: str, count: int = 50) -> list[dict]:
    """Get top token holders."""
    holders = []
    pages = (count + 9) // 10
    for page in range(1, pages + 1):
        data = await bscscan_request({
            "module": "token",
            "action": "tokenholderlist",
            "contractaddress": contract_address,
            "page": str(page),
            "offset": "10",
        })
        if data and isinstance(data.get("result"), list):
            holders.extend(data["result"])
        else:
            break
    return holders


async def get_funding_source(wallet_address: str) -> str | None:
    """Trace original funding source of a wallet."""
    data = await bscscan_request({
        "module": "account",
        "action": "txlist",
        "address": wallet_address,
        "startblock": "0",
        "endblock": "99999999",
        "page": "1",
        "offset": "5",
        "sort": "asc",
    })
    if data and isinstance(data.get("result"), list):
        wallet_lower = wallet_address.lower()
        for tx in data["result"]:
            if tx.get("to", "").lower() == wallet_lower and float(tx.get("value", 0)) > 0:
                return tx.get("from", "").lower()
    return None


async def seed_wallets_for_token(
    token_info: dict, db: Session
) -> int:
    """
    Seed known wallets for a single confirmed pump token.
    Returns number of wallets added.
    """
    contract = token_info["contract"]
    symbol = token_info["symbol"]

    if not contract:
        logger.warning(f"Skipping {symbol} — no contract address")
        return 0

    added = 0

    # 1. Get deployer
    deployer = await get_deployer(contract)
    if deployer:
        existing = db.query(KnownWallet).filter_by(wallet_address=deployer).first()
        if not existing:
            wallet = KnownWallet(
                wallet_address=deployer,
                label=f"{symbol} deployer",
                associated_token=symbol,
                associated_contract=contract,
                role="deployer",
                is_active=True,
                added_at=datetime.utcnow(),
            )
            db.add(wallet)
            added += 1
            logger.info(f"Added deployer for {symbol}: {deployer}")

    # 2. Get top holders and trace their funding
    holders = await get_top_holders(contract, count=50)
    funding_map: dict[str, list[str]] = defaultdict(list)

    for holder in holders:
        holder_addr = holder.get("TokenHolderAddress", "").lower()
        if not holder_addr:
            continue

        # Trace funding source
        funder = await get_funding_source(holder_addr)
        if funder:
            funding_map[funder].append(holder_addr)

        # Add as known wallet
        existing = db.query(KnownWallet).filter_by(wallet_address=holder_addr).first()
        if not existing:
            wallet = KnownWallet(
                wallet_address=holder_addr,
                label=f"{symbol} top holder",
                associated_token=symbol,
                associated_contract=contract,
                role="accumulator",
                funding_source=funder,
                is_active=True,
                added_at=datetime.utcnow(),
            )
            db.add(wallet)
            added += 1

    # 3. Identify cluster members (shared funding source)
    for funder, funded_wallets in funding_map.items():
        if len(funded_wallets) >= 3:
            logger.warning(
                f"Cluster detected for {symbol}: {funder} funded {len(funded_wallets)} wallets"
            )
            for addr in funded_wallets:
                wallet = db.query(KnownWallet).filter_by(wallet_address=addr).first()
                if wallet:
                    wallet.role = "cluster_member"
                    wallet.notes = f"Cluster funded by {funder}"

    db.commit()
    return added


async def run_wallet_seeder():
    """Seed known wallets from all confirmed pump tokens."""
    logger.info("Starting wallet seeder...")
    db = SessionLocal()
    total_added = 0

    try:
        for token_info in CONFIRMED_PUMPS:
            try:
                count = await seed_wallets_for_token(token_info, db)
                total_added += count
                logger.info(f"Seeded {count} wallets from {token_info['symbol']}")
            except Exception as e:
                logger.error(f"Error seeding {token_info['symbol']}: {e}")
                continue

        logger.info(f"Wallet seeder complete. Added {total_added} wallets total.")

    except Exception as e:
        logger.error(f"Wallet seeder error: {e}")
    finally:
        db.close()
