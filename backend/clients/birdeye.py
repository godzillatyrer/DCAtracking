"""Birdeye client — Solana token metadata + security.

Free tier at birdeye.so gives ~1k req/day. Used as a fallback to GMGN
and for token security checks on Solana.
"""

import asyncio
import logging
from typing import Any

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)


class BirdeyeClient:
    def __init__(self):
        self.base = settings.BIRDEYE_BASE_URL.rstrip("/")
        self.key = settings.BIRDEYE_API_KEY

    @property
    def configured(self) -> bool:
        return bool(self.key)

    async def _get(self, path: str, params: dict | None = None) -> Any | None:
        if not self.configured:
            return None
        url = f"{self.base}{path}"
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    url,
                    params=params,
                    headers={"X-API-KEY": self.key, "x-chain": "solana"},
                )
                await asyncio.sleep(0.1)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, dict) and data.get("success"):
                        return data.get("data")
                    return data
                logger.warning(f"Birdeye {path} -> {resp.status_code}")
        except Exception as e:
            logger.error(f"Birdeye error on {path}: {e}")
        return None

    async def token_overview(self, address: str) -> dict | None:
        return await self._get("/defi/token_overview", {"address": address})

    async def token_security(self, address: str) -> dict | None:
        return await self._get("/defi/token_security", {"address": address})

    async def token_holders(self, address: str, limit: int = 100) -> list[dict]:
        data = await self._get(
            "/defi/v3/token/holder",
            {"address": address, "limit": limit},
        )
        if isinstance(data, dict):
            return data.get("items") or []
        return data or []


birdeye = BirdeyeClient()
