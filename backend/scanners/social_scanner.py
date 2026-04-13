"""
Step 5: Social Scanner — Social signal monitoring.

Monitors Twitter/X and CoinGecko trending for social signal ramp-up.
Phase 1: Uses CoinGecko trending as a free proxy for social activity.
Phase 2: Optional Twitter API integration.
Runs every 2 hours for watchlist tokens.
"""

import logging
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import SessionLocal
from backend.models.flagged_token import FlaggedToken
from backend.models.watchlist import Watchlist

logger = logging.getLogger(__name__)

COINGECKO_TRENDING = "https://api.coingecko.com/api/v3/search/trending"
COINGECKO_SEARCH = "https://api.coingecko.com/api/v3/search"


async def fetch_coingecko_trending() -> list[dict]:
    """Fetch trending coins from CoinGecko (free, no key)."""
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(COINGECKO_TRENDING)
            if resp.status_code == 200:
                data = resp.json()
                coins = data.get("coins", [])
                return [c.get("item", {}) for c in coins]
        except Exception as e:
            logger.error(f"Error fetching CoinGecko trending: {e}")
    return []


async def search_coingecko(query: str) -> dict | None:
    """Search CoinGecko for a token by name or symbol."""
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(COINGECKO_SEARCH, params={"query": query})
            if resp.status_code == 200:
                data = resp.json()
                coins = data.get("coins", [])
                if coins:
                    return coins[0]
        except Exception as e:
            logger.error(f"Error searching CoinGecko: {e}")
    return None


async def search_twitter_mentions(
    symbol: str, name: str, bearer_token: str
) -> dict:
    """
    Search Twitter for token mentions.
    Requires Twitter API bearer token (optional Phase 2).
    """
    result = {"mention_count": 0, "tweets": [], "velocity_increase": False}

    if not bearer_token:
        return result

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            headers = {"Authorization": f"Bearer {bearer_token}"}
            query = f"({symbol} OR {name}) -is:retweet lang:en"
            resp = await client.get(
                "https://api.twitter.com/2/tweets/search/recent",
                params={
                    "query": query,
                    "max_results": 50,
                    "tweet.fields": "created_at,public_metrics,author_id",
                },
                headers=headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                tweets = data.get("data", [])
                result["mention_count"] = len(tweets)
                result["tweets"] = tweets
                # Simple velocity heuristic: > 20 mentions = signal
                result["velocity_increase"] = len(tweets) > 20
        except Exception as e:
            logger.error(f"Error searching Twitter for {symbol}: {e}")

    return result


async def check_social_signals(
    token_symbol: str, token_name: str
) -> dict:
    """
    Check social signals for a token across available platforms.
    Returns social analysis results.
    """
    result = {
        "social_signal_detected": False,
        "mention_count": 0,
        "is_trending_coingecko": False,
        "twitter_mentions": 0,
    }

    # Check CoinGecko trending
    trending = await fetch_coingecko_trending()
    for coin in trending:
        if (
            coin.get("symbol", "").lower() == token_symbol.lower()
            or coin.get("name", "").lower() == token_name.lower()
        ):
            result["is_trending_coingecko"] = True
            result["social_signal_detected"] = True
            result["mention_count"] += 1
            break

    # Search CoinGecko for the token
    cg_result = await search_coingecko(token_symbol)
    if cg_result:
        # Token exists on CoinGecko — could check market_cap_rank for trending
        rank = cg_result.get("market_cap_rank")
        if rank and rank < 500:
            result["social_signal_detected"] = True

    # Twitter search (Phase 2 — only if bearer token configured)
    if settings.TWITTER_BEARER_TOKEN:
        twitter = await search_twitter_mentions(
            token_symbol, token_name, settings.TWITTER_BEARER_TOKEN
        )
        result["twitter_mentions"] = twitter["mention_count"]
        result["mention_count"] += twitter["mention_count"]
        if twitter["velocity_increase"]:
            result["social_signal_detected"] = True

    return result


async def run_social_scanner():
    """Main social scanner entry point. Called every 2 hours."""
    logger.info("Starting social scanner run...")
    db = SessionLocal()
    scanned_count = 0

    try:
        # Scan watchlist tokens
        watchlist_tokens = db.query(FlaggedToken).filter(
            FlaggedToken.status == "watchlist",
            FlaggedToken.removed.is_(False),
        ).all()

        logger.info(f"Scanning social signals for {len(watchlist_tokens)} tokens")

        for token in watchlist_tokens:
            try:
                signals = await check_social_signals(
                    token.token_symbol or "", token.token_name or ""
                )

                # Update watchlist entry
                entry = db.query(Watchlist).filter_by(
                    contract_address=token.contract_address
                ).first()
                if entry:
                    entry.social_signal_detected = signals["social_signal_detected"]
                    entry.social_mention_count = signals["mention_count"]
                    db.commit()

                scanned_count += 1

                if signals["social_signal_detected"]:
                    logger.info(
                        f"Social signal detected: {token.token_symbol} — "
                        f"{signals['mention_count']} mentions"
                    )

            except Exception as e:
                logger.error(f"Error scanning social for {token.token_symbol}: {e}")
                continue

        logger.info(f"Social scanner complete. Scanned {scanned_count} tokens.")

    except Exception as e:
        logger.error(f"Social scanner error: {e}")
    finally:
        db.close()
