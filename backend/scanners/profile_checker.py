"""
Step 2: Token Profile Checker — BscScan token enrichment.

Enriches flagged tokens with supply data, holder concentration,
contract metadata, and narrative classification.
Runs every 1 hour for tokens with status='raw'.
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
from backend.models.token_profile import TokenProfile

logger = logging.getLogger(__name__)

BSCSCAN_BASE = settings.BSCSCAN_BASE_URL
API_KEY = settings.BSCSCAN_API_KEY
RATE_LIMIT_DELAY = 0.25  # 250ms between requests (5 calls/sec max)

# Narrative keywords for classification
NARRATIVE_KEYWORDS = {
    "AI": ["ai", "artificial", "intelligence", "neural", "machine learning", "gpt", "llm"],
    "gaming": ["game", "gaming", "play", "metaverse", "nft game", "p2e", "play-to-earn"],
    "DeFi": ["defi", "swap", "yield", "stake", "lending", "liquidity", "amm", "dex"],
    "meme": ["doge", "pepe", "shib", "meme", "inu", "moon", "elon"],
    "RWA": ["rwa", "real world", "asset", "tokenized", "treasury"],
    "Web3": ["web3", "dao", "governance", "decentralized"],
    "L2": ["layer 2", "l2", "rollup", "scaling", "bridge"],
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
                if data.get("status") == "1" or data.get("result"):
                    return data
        except Exception as e:
            logger.error(f"BscScan API error: {e}")
    return None


async def get_token_supply(contract_address: str) -> Decimal | None:
    """Get total token supply."""
    data = await bscscan_request({
        "module": "stats",
        "action": "tokensupply",
        "contractaddress": contract_address,
    })
    if data and data.get("result"):
        try:
            return Decimal(data["result"])
        except Exception:
            pass
    return None


async def get_contract_creator(contract_address: str) -> str | None:
    """Get the contract deployer address."""
    data = await bscscan_request({
        "module": "contract",
        "action": "getcontractcreation",
        "contractaddresses": contract_address,
    })
    if data and isinstance(data.get("result"), list) and len(data["result"]) > 0:
        return data["result"][0].get("contractCreator", "").lower()
    return None


async def get_contract_source(contract_address: str) -> dict:
    """Get contract source code and check for red flags."""
    data = await bscscan_request({
        "module": "contract",
        "action": "getsourcecode",
        "address": contract_address,
    })
    result = {
        "is_verified": False,
        "has_proxy": False,
        "has_mint": False,
        "has_pause": False,
        "has_blacklist": False,
        "source_code": "",
    }
    if data and isinstance(data.get("result"), list) and len(data["result"]) > 0:
        source_info = data["result"][0]
        source_code = source_info.get("SourceCode", "")
        result["is_verified"] = bool(source_code)
        result["source_code"] = source_code
        result["has_proxy"] = bool(source_info.get("Proxy", "0") != "0")

        source_lower = source_code.lower()
        result["has_mint"] = "mint" in source_lower and "function" in source_lower
        result["has_pause"] = "pause" in source_lower or "unpause" in source_lower
        result["has_blacklist"] = (
            "blacklist" in source_lower
            or "blocklist" in source_lower
            or "isblacklisted" in source_lower
        )
    return result


async def get_token_holders(contract_address: str) -> list[dict]:
    """
    Get top token holders. Uses tokentx to approximate if holder list endpoint unavailable.
    """
    data = await bscscan_request({
        "module": "token",
        "action": "tokenholderlist",
        "contractaddress": contract_address,
        "page": "1",
        "offset": "10",
    })
    if data and isinstance(data.get("result"), list):
        return data["result"]
    return []


async def get_token_info(contract_address: str) -> dict | None:
    """Get token metadata info."""
    data = await bscscan_request({
        "module": "token",
        "action": "tokeninfo",
        "contractaddress": contract_address,
    })
    if data and isinstance(data.get("result"), list) and len(data["result"]) > 0:
        return data["result"][0]
    return None


def classify_narrative(token_name: str, token_symbol: str, description: str = "") -> list[str]:
    """Classify token narrative based on name/description keywords."""
    text = f"{token_name} {token_symbol} {description}".lower()
    tags = []
    for narrative, keywords in NARRATIVE_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            tags.append(narrative)
    return tags


def calculate_float_pct(circulating: Decimal | None, max_supply: Decimal | None) -> Decimal | None:
    """Calculate float percentage."""
    if circulating and max_supply and max_supply > 0:
        return (circulating / max_supply * 100).quantize(Decimal("0.01"))
    return None


def calculate_holder_concentration(holders: list[dict], total_supply: Decimal | None) -> dict:
    """Calculate holder concentration metrics."""
    result = {"top_10_pct": None, "top_1_pct": None}
    if not holders or not total_supply or total_supply <= 0:
        return result

    try:
        balances = []
        for h in holders[:10]:
            bal = Decimal(h.get("TokenHolderQuantity", "0"))
            balances.append(bal)

        if balances:
            top_10_total = sum(balances)
            result["top_10_pct"] = (top_10_total / total_supply * 100).quantize(Decimal("0.01"))
            result["top_1_pct"] = (balances[0] / total_supply * 100).quantize(Decimal("0.01"))
    except Exception as e:
        logger.error(f"Error calculating holder concentration: {e}")

    return result


async def enrich_token(contract_address: str, db: Session) -> bool:
    """Enrich a single token with profile data. Returns True if successful."""
    flagged = db.query(FlaggedToken).filter_by(contract_address=contract_address).first()
    if not flagged:
        return False

    logger.info(f"Enriching {flagged.token_symbol} ({contract_address[:10]}...)")

    # Fetch data in parallel-safe sequential manner (rate limited)
    total_supply = await get_token_supply(contract_address)
    deployer = await get_contract_creator(contract_address)
    contract_info = await get_contract_source(contract_address)
    holders = await get_token_holders(contract_address)
    token_info = await get_token_info(contract_address)

    # Calculate metrics
    circulating = total_supply  # Approximation — refine with locked/burned token subtraction
    float_pct = calculate_float_pct(circulating, total_supply)
    concentration = calculate_holder_concentration(holders, total_supply)

    # Token age
    token_age_days = None
    if flagged.pair_created_at:
        token_age_days = (datetime.utcnow() - flagged.pair_created_at).days

    # Narrative
    description = ""
    if token_info:
        description = token_info.get("description", "")
    tags = classify_narrative(flagged.token_name or "", flagged.token_symbol or "", description)

    # Upsert profile
    existing_profile = db.query(TokenProfile).filter_by(contract_address=contract_address).first()
    profile_data = {
        "max_supply": total_supply,
        "circulating_supply": circulating,
        "float_pct": float_pct,
        "token_age_days": token_age_days,
        "top_10_holder_pct": concentration["top_10_pct"],
        "top_1_holder_pct": concentration["top_1_pct"],
        "deployer_address": deployer,
        "is_contract_verified": contract_info["is_verified"],
        "has_proxy": contract_info["has_proxy"],
        "has_mint_function": contract_info["has_mint"],
        "has_pause_function": contract_info["has_pause"],
        "has_blacklist_function": contract_info["has_blacklist"],
        "narrative_tags": tags,
        "updated_at": datetime.utcnow(),
    }

    if existing_profile:
        for key, value in profile_data.items():
            setattr(existing_profile, key, value)
    else:
        profile = TokenProfile(contract_address=contract_address, **profile_data)
        db.add(profile)

    db.commit()
    return True


async def run_profile_checker():
    """Main profile checker entry point. Called every 1 hour."""
    logger.info("Starting profile checker run...")
    db = SessionLocal()
    enriched_count = 0

    try:
        raw_tokens = db.query(FlaggedToken).filter(
            FlaggedToken.status == "raw",
            FlaggedToken.removed.is_(False),
        ).all()

        logger.info(f"Found {len(raw_tokens)} raw tokens to enrich")

        for token in raw_tokens:
            try:
                success = await enrich_token(token.contract_address, db)
                if success:
                    enriched_count += 1
            except Exception as e:
                logger.error(f"Error enriching {token.contract_address}: {e}")
                continue

        logger.info(f"Profile checker complete. Enriched {enriched_count} tokens.")

    except Exception as e:
        logger.error(f"Profile checker error: {e}")
    finally:
        db.close()
