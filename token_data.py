"""Token price, market cap, and volume data from Jupiter Price API + GeckoTerminal."""

import logging

import aiohttp

import config

logger = logging.getLogger(__name__)


async def get_token_prices_batch(session: aiohttp.ClientSession,
                                 mints: list[str]) -> dict:
    """Get prices for multiple tokens at once from Jupiter Price API."""
    if not mints:
        return {}

    try:
        ids = ",".join(mints)
        url = f"{config.JUPITER_PRICE_API}?ids={ids}"
        async with session.get(url) as resp:
            if resp.status != 200:
                logger.debug("Jupiter price API returned %d", resp.status)
                return {}
            data = await resp.json()
            result = {}
            for mint, info in data.get("data", {}).items():
                result[mint] = {
                    "price_usd": float(info.get("price", 0)),
                    "symbol": info.get("mintSymbol", ""),
                }
            return result
    except Exception as e:
        logger.debug("Jupiter batch price fetch failed: %s", e)
        return {}


async def get_token_price(session: aiohttp.ClientSession,
                          mint: str) -> dict | None:
    """Get token price from Jupiter Price API."""
    prices = await get_token_prices_batch(session, [mint])
    return prices.get(mint)


async def get_token_info_gecko(session: aiohttp.ClientSession,
                               mint: str,
                               network: str = "solana") -> dict | None:
    """Get token info from GeckoTerminal (includes volume, mcap, liquidity)."""
    try:
        url = (
            f"{config.GECKO_TERMINAL_BASE_URL}/networks/{network}"
            f"/tokens/{mint}?include=top_pools"
        )
        headers = {"Accept": "application/json"}
        async with session.get(url, headers=headers) as resp:
            if resp.status == 404:
                return None
            if resp.status == 429:
                logger.debug("GeckoTerminal rate limited for %s", mint[:16])
                return None
            if resp.status != 200:
                logger.debug("GeckoTerminal returned %d for %s", resp.status, mint[:16])
                return None

            data = await resp.json()
            attrs = data.get("data", {}).get("attributes", {})

            if not attrs:
                return None

            # Extract volume from top pool if available
            volume_24h = 0.0
            liquidity = 0.0

            included = data.get("included", [])
            if included:
                pool_attrs = included[0].get("attributes", {})
                vol_data = pool_attrs.get("volume_usd", {})
                volume_24h = float(vol_data.get("h24", 0) or 0)
                liquidity = float(pool_attrs.get("reserve_in_usd", 0) or 0)

            mcap = float(attrs.get("market_cap_usd") or attrs.get("fdv_usd") or 0)
            price = float(attrs.get("price_usd") or 0)

            return {
                "mint": mint,
                "name": attrs.get("name", ""),
                "symbol": attrs.get("symbol", ""),
                "price_usd": price,
                "market_cap": mcap,
                "volume_24h": volume_24h,
                "liquidity_usd": liquidity,
            }
    except Exception as e:
        logger.debug("GeckoTerminal fetch failed for %s: %s", mint[:16], e)
        return None


async def get_token_full_data(session: aiohttp.ClientSession,
                              mint: str) -> dict | None:
    """Get comprehensive token data by combining Jupiter + GeckoTerminal."""
    gecko_data = await get_token_info_gecko(session, mint)
    jup_data = await get_token_price(session, mint)

    if not gecko_data and not jup_data:
        return None

    result = {
        "mint": mint,
        "symbol": "",
        "name": "",
        "price_usd": 0,
        "market_cap": 0,
        "volume_24h": 0,
        "liquidity_usd": 0,
    }

    if gecko_data:
        result.update({
            "symbol": gecko_data.get("symbol", ""),
            "name": gecko_data.get("name", ""),
            "price_usd": gecko_data.get("price_usd", 0),
            "market_cap": gecko_data.get("market_cap", 0),
            "volume_24h": gecko_data.get("volume_24h", 0),
            "liquidity_usd": gecko_data.get("liquidity_usd", 0),
        })

    # Jupiter price is more real-time, prefer it when available
    if jup_data:
        result["price_usd"] = jup_data.get("price_usd", result["price_usd"])
        if not result["symbol"]:
            result["symbol"] = jup_data.get("symbol", "")

    return result


def convert_raw_amount(raw_amount: int, mint: str) -> float:
    """Convert raw token amount to human-readable using known decimals."""
    if mint in config.KNOWN_BASE_MINTS:
        decimals = config.KNOWN_BASE_MINTS[mint]["decimals"]
        return raw_amount / (10 ** decimals)
    # Default to 9 decimals (most SPL tokens on Solana)
    return raw_amount / 1e9


async def get_dca_value_usd(session: aiohttp.ClientSession,
                            input_mint: str, raw_amount: int) -> float:
    """Calculate the USD value of a DCA order's input amount."""
    human_amount = convert_raw_amount(raw_amount, input_mint)

    price_data = await get_token_price(session, input_mint)
    if price_data and price_data["price_usd"] > 0:
        return human_amount * price_data["price_usd"]

    # Stablecoins are ~$1
    if input_mint in (
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    ):
        return human_amount

    return 0.0
