"""
Wallet Seeder — Seed known wallets from confirmed pump tokens.

Extracts top holders, deployers, and cluster patterns from the 5 confirmed
pump tokens and populates the known_wallets table.

Works with BscScan FREE tier only — no Arkham needed.
Uses two strategies to find top holders:
  1. tokenholderlist API (may require Pro)
  2. Fallback: reconstruct holders from tokentx transfer history (always free)
"""

import asyncio
import logging
from datetime import datetime
from collections import defaultdict
from decimal import Decimal

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

# Known exchange/router addresses to EXCLUDE from holder reconstruction
# These are not insider wallets — they're infrastructure
EXCLUDED_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",  # burn/zero address
    "0x000000000000000000000000000000000000dead",  # dead address
    "0x10ed43c718714eb63d5aa57b78b54704e256024e",  # PancakeSwap Router v2
    "0x13f4ea83d0bd40e75c8222255bc855a974568dd4",  # PancakeSwap Router v3
    "0x1b81d678ffb9c0263b24a97847620c99d213eb14",  # PancakeSwap position mgr
    "0x8894e0a0c962cb723c1ef8a1b5b5b174d81bd981",  # Binance Hot Wallet
    "0xe2fc31f816a9b94326492132018c3aecc4a93ae1",  # Binance Hot Wallet 2
    "0xf977814e90da44bfa03b6295a0616a897441acec",  # Binance Hot Wallet 3
    "0x28c6c06298d514db089934071355e5743bf21d60",  # Binance Hot Wallet 4
}


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
    """Get top token holders via tokenholderlist API (may need Pro)."""
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


async def reconstruct_holders_from_transfers(
    contract_address: str, max_pages: int = 10
) -> list[dict]:
    """
    FALLBACK: Reconstruct top holders by replaying all token transfers.
    This uses the tokentx endpoint which is ALWAYS available on BscScan free tier.
    We pull transfer history and compute net balances per wallet.
    """
    logger.info(f"Reconstructing holders from transfer history for {contract_address[:10]}...")
    balances: dict[str, Decimal] = defaultdict(Decimal)

    for page in range(1, max_pages + 1):
        data = await bscscan_request({
            "module": "account",
            "action": "tokentx",
            "contractaddress": contract_address,
            "page": str(page),
            "offset": "1000",
            "sort": "asc",
        })
        if not data or not isinstance(data.get("result"), list):
            break

        transfers = data["result"]
        if not transfers:
            break

        for tx in transfers:
            from_addr = tx.get("from", "").lower()
            to_addr = tx.get("to", "").lower()
            decimals = int(tx.get("tokenDecimal", "18"))
            try:
                amount = Decimal(tx.get("value", "0")) / Decimal(10**decimals)
            except Exception:
                continue

            if from_addr:
                balances[from_addr] -= amount
            if to_addr:
                balances[to_addr] += amount

        logger.info(f"  Page {page}: processed {len(transfers)} transfers, {len(balances)} wallets")

        # If we got fewer than 1000 results, we've reached the end
        if len(transfers) < 1000:
            break

    # Sort by balance descending, exclude zero/negative and known infrastructure
    sorted_holders = sorted(
        [(addr, bal) for addr, bal in balances.items()
         if bal > 0 and addr not in EXCLUDED_ADDRESSES],
        key=lambda x: x[1],
        reverse=True,
    )

    # Return top 50 in the same format as tokenholderlist API
    return [
        {"TokenHolderAddress": addr, "TokenHolderQuantity": str(bal)}
        for addr, bal in sorted_holders[:50]
    ]


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

    # 2. Get top holders — try API first, fallback to transfer reconstruction
    holders = await get_top_holders(contract, count=50)
    if not holders:
        logger.info(f"tokenholderlist unavailable for {symbol}, reconstructing from transfers...")
        holders = await reconstruct_holders_from_transfers(contract)
    logger.info(f"Found {len(holders)} holders for {symbol}")

    # 3. Trace funding sources for cluster detection
    funding_map: dict[str, list[str]] = defaultdict(list)

    for holder in holders:
        holder_addr = holder.get("TokenHolderAddress", "").lower()
        if not holder_addr or holder_addr in EXCLUDED_ADDRESSES:
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

    # 4. Identify cluster members (shared funding source)
    for funder, funded_wallets in funding_map.items():
        if len(funded_wallets) >= 3:
            logger.warning(
                f"CLUSTER DETECTED for {symbol}: {funder} funded {len(funded_wallets)} wallets"
            )
            for addr in funded_wallets:
                wallet = db.query(KnownWallet).filter_by(wallet_address=addr).first()
                if wallet:
                    wallet.role = "cluster_member"
                    wallet.notes = f"Cluster funded by {funder}"

            # Also add the funder itself as a known wallet
            existing = db.query(KnownWallet).filter_by(wallet_address=funder).first()
            if not existing and funder not in EXCLUDED_ADDRESSES:
                wallet = KnownWallet(
                    wallet_address=funder,
                    label=f"{symbol} cluster funder ({len(funded_wallets)} wallets)",
                    associated_token=symbol,
                    associated_contract=contract,
                    role="distributor",
                    is_active=True,
                    added_at=datetime.utcnow(),
                    notes=f"Funded wallets: {', '.join(w[:10] for w in funded_wallets)}",
                )
                db.add(wallet)
                added += 1

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
                logger.info(f"\n{'='*50}")
                logger.info(f"Processing {token_info['symbol']} ({token_info['name']})")
                logger.info(f"Contract: {token_info['contract']}")
                logger.info(f"{'='*50}")
                count = await seed_wallets_for_token(token_info, db)
                total_added += count
                logger.info(f"Seeded {count} wallets from {token_info['symbol']}")
            except Exception as e:
                logger.error(f"Error seeding {token_info['symbol']}: {e}")
                continue

        logger.info(f"\nWallet seeder complete. Added {total_added} wallets total.")

    except Exception as e:
        logger.error(f"Wallet seeder error: {e}")
    finally:
        db.close()
