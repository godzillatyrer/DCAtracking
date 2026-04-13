"""
Shared BscScan API client — single bscscan_request function used by all scanners.
Uses Etherscan V2 API with chainid=56 for BSC.
"""

import asyncio
import logging

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)

RATE_LIMIT_DELAY = 0.25  # 250ms between requests (5 calls/sec max for free tier)


async def bscscan_request(params: dict) -> dict | None:
    """
    Make a rate-limited request to the BscScan (Etherscan V2) API.
    Automatically injects apikey and chainid.
    Returns parsed JSON response or None on failure.
    """
    params["apikey"] = settings.BSCSCAN_API_KEY
    params["chainid"] = settings.BSCSCAN_CHAIN_ID
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(settings.BSCSCAN_BASE_URL, params=params)
            await asyncio.sleep(RATE_LIMIT_DELAY)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "1" or (
                    isinstance(data.get("result"), (list, str)) and data.get("message") != "NOTOK"
                ):
                    return data
                else:
                    logger.warning(
                        f"BscScan API non-OK: message={data.get('message')} "
                        f"result={str(data.get('result', ''))[:100]}"
                    )
            else:
                logger.error(f"BscScan HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.error(f"BscScan API error: {e}")
    return None
