"""GeckoTerminal client — new pools, trending, OHLCV.

Free, no key required. Used for:
  - New pair discovery (Module 4/5/9 seeding)
  - Backfilling historical pump tokens during golden-deployer seeding
  - Cross-checking DEX Screener

Docs: https://www.geckoterminal.com/dex-api
Rate limit: 30 req/min (generous enough for our use).
"""

import asyncio
import logging
from typing import Any

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)


# GeckoTerminal chain slugs we support
CHAIN_SLUGS = {
    "bsc": "bsc",
    "ethereum": "eth",
    "base": "base",
    "solana": "solana",
}


class GeckoTerminalClient:
    """Async wrapper around GeckoTerminal public endpoints."""

    def __init__(self):
        self.base = settings.GECKOTERMINAL_BASE_URL.rstrip("/")

    @property
    def configured(self) -> bool:
        return True

    async def _get(self, path: str, params: dict | None = None) -> Any | None:
        url = f"{self.base}{path}"
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    return resp.json()
                logger.warning(f"GeckoTerminal {path} -> {resp.status_code}")
        except Exception as e:
            logger.error(f"GeckoTerminal error on {path}: {e}")
        await asyncio.sleep(0.3)  # 30 req/min = 2s gap; we batch so 300ms is OK
        return None

    async def new_pools(self, chain: str = "bsc", limit: int = 100) -> list[dict]:
        """Latest pools on a chain — Module 4/5/9 seeding."""
        slug = CHAIN_SLUGS.get(chain, chain)
        data = await self._get(f"/networks/{slug}/new_pools", {"page": 1})
        if not data:
            return []
        return (data.get("data") or [])[:limit]

    async def trending_pools(self, chain: str = "bsc", limit: int = 100) -> list[dict]:
        slug = CHAIN_SLUGS.get(chain, chain)
        data = await self._get(f"/networks/{slug}/trending_pools", {"page": 1})
        if not data:
            return []
        return (data.get("data") or [])[:limit]

    async def token_info(self, chain: str, address: str) -> dict | None:
        slug = CHAIN_SLUGS.get(chain, chain)
        data = await self._get(f"/networks/{slug}/tokens/{address}")
        return (data or {}).get("data") if data else None

    async def pool_info(self, chain: str, pool_address: str) -> dict | None:
        slug = CHAIN_SLUGS.get(chain, chain)
        data = await self._get(f"/networks/{slug}/pools/{pool_address}")
        return (data or {}).get("data") if data else None


geckoterminal = GeckoTerminalClient()
