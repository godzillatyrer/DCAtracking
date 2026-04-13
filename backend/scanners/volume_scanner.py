"""
Step 1: Volume Scanner — DEX Screener integration.

Scans for BSC tokens with unusual volume patterns every 15 minutes.
Flags tokens meeting volume spike, new pair, or high vol/mcap ratio criteria.
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import SessionLocal
from backend.models.flagged_token import FlaggedToken

logger = logging.getLogger(__name__)

DEXSCREENER_TRENDING = f"{settings.DEXSCREENER_BASE_URL}/token-boosts/top/v1"
DEXSCREENER_PROFILES = f"{settings.DEXSCREENER_BASE_URL}/token-profiles/latest/v1"
DEXSCREENER_TOKENS = f"{settings.DEXSCREENER_BASE_URL}/latest/dex/tokens"
DEXSCREENER_SEARCH = f"{settings.DEXSCREENER_BASE_URL}/latest/dex/search"

# Volume thresholds
MIN_VOLUME_FLAG = 500_000  # $500K minimum 24h volume for initial flag
VOLUME_SPIKE_5X = 5
VOLUME_SPIKE_20X = 20
VOLUME_MCAP_RATIO_50 = 0.50
VOLUME_MCAP_RATIO_100 = 1.00
NEW_PAIR_MAX_AGE_HOURS = 48
NEW_PAIR_MIN_VOLUME = 100_000


async def fetch_trending_tokens() -> list[dict]:
    """Fetch trending/boosted tokens from DEX Screener."""
    tokens = []
    async with httpx.AsyncClient(timeout=30) as client:
        # Fetch boosted tokens
        try:
            resp = await client.get(DEXSCREENER_TRENDING)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    tokens.extend(data)
        except Exception as e:
            logger.error(f"Error fetching trending tokens: {e}")

        # Fetch latest token profiles
        try:
            resp = await client.get(DEXSCREENER_PROFILES)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    tokens.extend(data)
        except Exception as e:
            logger.error(f"Error fetching token profiles: {e}")

    return tokens


async def fetch_token_pairs(token_address: str) -> list[dict]:
    """Fetch pair data for a specific token address."""
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(f"{DEXSCREENER_TOKENS}/{token_address}")
            if resp.status_code == 200:
                data = resp.json()
                return data.get("pairs", [])
        except Exception as e:
            logger.error(f"Error fetching token pairs for {token_address}: {e}")
    return []


def filter_bsc_pairs(pairs: list[dict]) -> list[dict]:
    """Filter pairs to BSC chain only."""
    return [p for p in pairs if p.get("chainId") == "bsc"]


def evaluate_pair(pair: dict, db: Session) -> dict | None:
    """
    Evaluate a single pair against flagging criteria.
    Returns token data dict if it should be flagged, None otherwise.
    """
    try:
        volume_24h = float(pair.get("volume", {}).get("h24", 0) or 0)
        market_cap = float(pair.get("marketCap", 0) or 0)
        fdv = float(pair.get("fdv", 0) or 0)
        liquidity_usd = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        price_usd = float(pair.get("priceUsd", 0) or 0)
        pair_created_str = pair.get("pairCreatedAt")
        base_token = pair.get("baseToken", {})
        contract_address = base_token.get("address", "").lower()
        token_name = base_token.get("name", "")
        token_symbol = base_token.get("symbol", "")

        if not contract_address:
            return None

        # Check volume threshold
        if volume_24h < MIN_VOLUME_FLAG:
            return None

        should_flag = False
        volume_change_pct = None

        # Check against stored historical data for volume spike
        existing = db.query(FlaggedToken).filter_by(contract_address=contract_address).first()
        if existing and existing.volume_24h and float(existing.volume_24h) > 0:
            avg_volume = float(existing.volume_24h)
            ratio = volume_24h / avg_volume
            volume_change_pct = (ratio - 1) * 100
            if ratio >= VOLUME_SPIKE_5X:
                should_flag = True
        else:
            # New token — flag if it meets base volume threshold
            should_flag = True

        # Check volume-to-market-cap ratio
        if market_cap > 0 and volume_24h / market_cap >= VOLUME_MCAP_RATIO_50:
            should_flag = True

        # Check new pair with high volume
        if pair_created_str:
            try:
                created_ms = int(pair_created_str)
                created_at = datetime.utcfromtimestamp(created_ms / 1000)
                age_hours = (datetime.utcnow() - created_at).total_seconds() / 3600
                if age_hours <= NEW_PAIR_MAX_AGE_HOURS and volume_24h >= NEW_PAIR_MIN_VOLUME:
                    should_flag = True
            except (ValueError, TypeError):
                created_at = None
        else:
            created_at = None

        if not should_flag:
            return None

        # Determine buyer counts from txns data
        txns = pair.get("txns", {})
        buys_24h = txns.get("h24", {}).get("buys", 0)

        return {
            "contract_address": contract_address,
            "token_name": token_name,
            "token_symbol": token_symbol,
            "chain": "bsc",
            "pair_address": pair.get("pairAddress", ""),
            "dex_url": pair.get("url", ""),
            "price_usd": Decimal(str(price_usd)),
            "volume_24h": Decimal(str(volume_24h)),
            "volume_change_pct": Decimal(str(volume_change_pct)) if volume_change_pct else None,
            "buyers_24h": buys_24h,
            "liquidity_usd": Decimal(str(liquidity_usd)),
            "market_cap": Decimal(str(market_cap)),
            "fdv": Decimal(str(fdv)),
            "pair_created_at": created_at,
        }

    except Exception as e:
        logger.error(f"Error evaluating pair: {e}")
        return None


def upsert_flagged_token(db: Session, token_data: dict):
    """Insert or update a flagged token record."""
    existing = db.query(FlaggedToken).filter_by(
        contract_address=token_data["contract_address"]
    ).first()

    if existing:
        for key, value in token_data.items():
            if key != "contract_address":
                setattr(existing, key, value)
        existing.last_seen_at = datetime.utcnow()
    else:
        token = FlaggedToken(
            **token_data,
            first_flagged_at=datetime.utcnow(),
            last_seen_at=datetime.utcnow(),
            status="raw",
        )
        db.add(token)

    db.commit()


def expire_old_flags(db: Session):
    """Mark tokens not seen for 7 days as expired."""
    cutoff = datetime.utcnow() - timedelta(days=7)
    db.query(FlaggedToken).filter(
        FlaggedToken.last_seen_at < cutoff,
        FlaggedToken.status.in_(["raw", "candidate"]),
    ).update({"status": "expired"}, synchronize_session=False)
    db.commit()


async def run_volume_scanner():
    """Main volume scanner entry point. Called every 15 minutes."""
    logger.info("Starting volume scanner run...")
    db = SessionLocal()
    flagged_count = 0

    try:
        # 1. Fetch trending tokens
        trending = await fetch_trending_tokens()
        logger.info(f"Fetched {len(trending)} trending/boosted tokens")

        # 2. Collect BSC token addresses from trending data
        bsc_addresses = set()
        for item in trending:
            chain = item.get("chainId", "")
            addr = item.get("tokenAddress", "")
            if chain == "bsc" and addr:
                bsc_addresses.add(addr.lower())

        logger.info(f"Found {len(bsc_addresses)} BSC token addresses to check")

        # 3. Fetch pair data for each address and evaluate
        for address in bsc_addresses:
            try:
                pairs = await fetch_token_pairs(address)
                bsc_pairs = filter_bsc_pairs(pairs)

                for pair in bsc_pairs:
                    token_data = evaluate_pair(pair, db)
                    if token_data:
                        upsert_flagged_token(db, token_data)
                        flagged_count += 1
                        logger.info(
                            f"Flagged: {token_data['token_symbol']} "
                            f"(vol=${token_data['volume_24h']:,.0f})"
                        )
            except Exception as e:
                logger.error(f"Error processing address {address}: {e}")
                continue

        # 4. Expire old flags
        expire_old_flags(db)

        logger.info(f"Volume scanner complete. Flagged {flagged_count} tokens.")

    except Exception as e:
        logger.error(f"Volume scanner error: {e}")
    finally:
        db.close()
