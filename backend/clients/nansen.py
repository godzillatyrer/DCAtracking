"""Nansen client — Smart Money labels + insider tracking.

Paid API (~$150/mo Standard tier at nansen.ai). If NANSEN_API_KEY is
unset, every method returns None/[] and dependent modules log-and-skip.

We expose a narrow surface: Smart Money label lookup for an address,
and "wallets that just bought token X". The Standard tier supports
more than this — expand as needed.

NOTE: Nansen's API surface changed in 2024. The URLs here are our best
reading of the docs as of the last refresh; update if Nansen moves them.
"""

import asyncio
import logging
from typing import Any

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)


class NansenClient:
    def __init__(self):
        self.base = settings.NANSEN_BASE_URL.rstrip("/")
        self.key = settings.NANSEN_API_KEY

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
                    headers={"apiKey": self.key, "accept": "application/json"},
                )
                await asyncio.sleep(0.1)
                if resp.status_code == 200:
                    return resp.json()
                logger.warning(f"Nansen {path} -> {resp.status_code}: {resp.text[:120]}")
        except Exception as e:
            logger.error(f"Nansen error on {path}: {e}")
        return None

    async def label_address(self, chain: str, address: str) -> dict | None:
        """
        Smart Money / entity labels for an address.
        Returns {labels: [...], entity: str, smart_money_tier: str, ...}
        """
        return await self._get(f"/address/{chain}/{address}/labels")

    async def token_smart_money(self, chain: str, token: str) -> list[dict]:
        """Wallets labeled as Smart Money that hold a given token."""
        data = await self._get(f"/token/{chain}/{token}/smart-money")
        if isinstance(data, dict):
            return data.get("data") or []
        return data or []

    async def address_pnl(self, chain: str, address: str) -> dict | None:
        """Historical PnL for an address — used to validate launcher track record."""
        return await self._get(f"/address/{chain}/{address}/pnl")


nansen = NansenClient()
