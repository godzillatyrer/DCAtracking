"""DexScreener API client for fetching token and pair data across chains."""

import asyncio
import logging
from typing import Optional

import aiohttp

import config

logger = logging.getLogger(__name__)

# Rate limiting: DexScreener free tier allows ~300 requests/minute
_semaphore = asyncio.Semaphore(5)


async def _fetch(session: aiohttp.ClientSession, url: str) -> Optional[dict]:
    async with _semaphore:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 429:
                    logger.warning("Rate limited by DexScreener, backing off")
                    await asyncio.sleep(5)
                    return None
                if resp.status != 200:
                    logger.warning("DexScreener returned %d for %s", resp.status, url)
                    return None
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error("Request failed for %s: %s", url, e)
            return None


async def get_boosted_tokens(session: aiohttp.ClientSession) -> list[dict]:
    """Get tokens that are currently being boosted (promoted) on DexScreener.
    Boosted tokens often indicate someone is paying to push visibility,
    which can be an early signal of a planned pump."""
    url = f"{config.DEXSCREENER_BASE_URL}/token-boosts/latest/v1"
    data = await _fetch(session, url)
    if not data:
        return []
    return data if isinstance(data, list) else []


async def get_top_boosted_tokens(session: aiohttp.ClientSession) -> list[dict]:
    """Get tokens with most active boosts - heavy promotion signals."""
    url = f"{config.DEXSCREENER_BASE_URL}/token-boosts/top/v1"
    data = await _fetch(session, url)
    if not data:
        return []
    return data if isinstance(data, list) else []


async def get_token_profiles(session: aiohttp.ClientSession) -> list[dict]:
    """Get latest token profiles (newly listed/updated tokens)."""
    url = f"{config.DEXSCREENER_BASE_URL}/token-profiles/latest/v1"
    data = await _fetch(session, url)
    if not data:
        return []
    return data if isinstance(data, list) else []


async def search_tokens(session: aiohttp.ClientSession,
                        query: str) -> list[dict]:
    """Search for tokens by name or symbol."""
    url = f"{config.DEXSCREENER_BASE_URL}/latest/dex/search?q={query}"
    data = await _fetch(session, url)
    if not data or "pairs" not in data:
        return []
    return data["pairs"]


async def get_pairs_by_chain(session: aiohttp.ClientSession,
                             chain: str, pair_addresses: list[str]) -> list[dict]:
    """Get detailed pair data for specific addresses on a chain."""
    if not pair_addresses:
        return []
    # API accepts up to 30 addresses comma-separated
    for i in range(0, len(pair_addresses), 30):
        batch = pair_addresses[i:i + 30]
        addresses = ",".join(batch)
        url = f"{config.DEXSCREENER_BASE_URL}/latest/dex/pairs/{chain}/{addresses}"
        data = await _fetch(session, url)
        if data and "pairs" in data and data["pairs"]:
            return data["pairs"]
    return []


async def get_token_pairs(session: aiohttp.ClientSession,
                          token_address: str) -> list[dict]:
    """Get all pairs for a specific token address across chains."""
    url = f"{config.DEXSCREENER_BASE_URL}/latest/dex/tokens/{token_address}"
    data = await _fetch(session, url)
    if not data or "pairs" not in data:
        return []
    return data["pairs"]


def parse_pair_data(pair: dict) -> Optional[dict]:
    """Extract relevant fields from a DexScreener pair object."""
    try:
        base_token = pair.get("baseToken", {})
        txns = pair.get("txns", {})
        price_change = pair.get("priceChange", {})
        volume = pair.get("volume", {})
        liquidity = pair.get("liquidity", {})

        price_usd_str = pair.get("priceUsd")
        price_usd = float(price_usd_str) if price_usd_str else 0.0

        return {
            "chain": pair.get("chainId", "unknown"),
            "pair_address": pair.get("pairAddress", ""),
            "token_address": base_token.get("address", ""),
            "token_symbol": base_token.get("symbol", "???"),
            "token_name": base_token.get("name", "Unknown"),
            "price_usd": price_usd,
            "market_cap": pair.get("marketCap") or pair.get("fdv") or 0,
            "liquidity_usd": liquidity.get("usd", 0) or 0,
            "volume_24h": volume.get("h24", 0) or 0,
            "volume_6h": volume.get("h6", 0) or 0,
            "volume_1h": volume.get("h1", 0) or 0,
            "price_change_24h": price_change.get("h24", 0) or 0,
            "price_change_6h": price_change.get("h6", 0) or 0,
            "price_change_1h": price_change.get("h1", 0) or 0,
            "buys_24h": txns.get("h24", {}).get("buys", 0) or 0,
            "sells_24h": txns.get("h24", {}).get("sells", 0) or 0,
            "buys_6h": txns.get("h6", {}).get("buys", 0) or 0,
            "sells_6h": txns.get("h6", {}).get("sells", 0) or 0,
            "buys_1h": txns.get("h1", {}).get("buys", 0) or 0,
            "sells_1h": txns.get("h1", {}).get("sells", 0) or 0,
            "dex_url": pair.get("url", ""),
        }
    except (ValueError, TypeError, KeyError) as e:
        logger.debug("Failed to parse pair data: %s", e)
        return None


async def discover_tokens(session: aiohttp.ClientSession) -> list[dict]:
    """Discover tokens from multiple sources and return parsed pair data.
    Focuses on low-cap tokens under MAX_MARKET_CAP."""

    all_pairs = []

    # Fetch from multiple discovery sources in parallel
    boosted, top_boosted, profiles = await asyncio.gather(
        get_boosted_tokens(session),
        get_top_boosted_tokens(session),
        get_token_profiles(session),
    )

    # Collect token addresses from boost/profile data to look up pairs
    token_addresses = set()
    for item in boosted + top_boosted:
        addr = item.get("tokenAddress")
        chain = item.get("chainId", "")
        if addr and chain in config.CHAINS:
            token_addresses.add(addr)

    for item in profiles:
        addr = item.get("tokenAddress")
        chain = item.get("chainId", "")
        if addr and chain in config.CHAINS:
            token_addresses.add(addr)

    # Fetch pair data for discovered tokens
    tasks = [get_token_pairs(session, addr) for addr in token_addresses]
    if tasks:
        results = await asyncio.gather(*tasks)
        for pairs in results:
            all_pairs.extend(pairs)

    # Also search for trending/new tokens on each chain
    search_tasks = [
        search_tokens(session, f"new {chain}")
        for chain in config.CHAINS
    ]
    search_results = await asyncio.gather(*search_tasks)
    for pairs in search_results:
        all_pairs.extend(pairs)

    # Parse and filter
    parsed = []
    seen = set()
    for pair in all_pairs:
        data = parse_pair_data(pair)
        if not data:
            continue
        # Deduplicate by token address + chain
        key = (data["token_address"], data["chain"])
        if key in seen:
            continue
        seen.add(key)

        # Filter: only tokens on our target chains
        if data["chain"] not in config.CHAINS:
            continue
        # Filter: under max market cap
        if data["market_cap"] > config.MAX_MARKET_CAP and data["market_cap"] > 0:
            continue
        # Filter: minimum liquidity (avoid pure rugs with $0 liquidity)
        if data["liquidity_usd"] < config.MIN_LIQUIDITY_USD:
            continue

        parsed.append(data)

    logger.info("Discovered %d tokens matching criteria", len(parsed))
    return parsed
