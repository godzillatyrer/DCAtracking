"""GMGN client — Solana smart-money tracker.

Free tier covers our needs. API key is optional on most endpoints.
Used to cross-reference Solana early buyers against known "smart money"
wallets.

Endpoints are under gmgn.ai/defi/quotation/v1. Responses use the
standard {code, data} envelope.
"""

import asyncio
import logging
from typing import Any

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)


class GMGNClient:
    def __init__(self):
        self.base = settings.GMGN_BASE_URL.rstrip("/")
        self.key = settings.GMGN_API_KEY

    @property
    def configured(self) -> bool:
        return True  # free tier works without key

    async def _get(self, path: str, params: dict | None = None) -> Any | None:
        url = f"{self.base}{path}"
        # Browser-like headers required to pass Cloudflare's bot
        # protection. Without these, GMGN returns 200 with empty data
        # for any token-specific query (observed: top_traders, top_holders,
        # smart_money_trades all return [] for Pump.fun tokens when called
        # from a server without browser headers).
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://gmgn.ai/",
            "Origin": "https://gmgn.ai",
        }
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(url, params=params, headers=headers)
                await asyncio.sleep(0.2)
                if resp.status_code == 200:
                    # Cloudflare sometimes returns HTML challenge page
                    # as 200 — detect by checking content-type.
                    ct = resp.headers.get("content-type", "")
                    if "html" in ct.lower():
                        logger.warning(f"GMGN {path}: Cloudflare HTML challenge")
                        return None
                    data = resp.json()
                    if isinstance(data, dict) and data.get("code") == 0:
                        return data.get("data")
                    return data
                logger.warning(f"GMGN {path} -> {resp.status_code}")
        except Exception as e:
            logger.error(f"GMGN error on {path}: {e}")
        return None

    async def token_holders(self, token_address: str, limit: int = 50) -> list[dict]:
        """Top holders of a Solana token with smart-money flags."""
        return await self._get(
            f"/tokens/top_holders/sol/{token_address}",
            {"limit": limit},
        ) or []

    async def token_top_traders(self, token_address: str, limit: int = 100) -> list[dict]:
        """Top traders of a token — ranked by PnL. Best source for
        extracting cabal wallets from a runner CA."""
        return await self._get(
            f"/tokens/top_traders/sol/{token_address}",
            {"limit": limit},
        ) or []

    async def smart_money_trades(self, token_address: str) -> list[dict]:
        """Recent trades on a token by GMGN-labeled smart money."""
        return await self._get(
            f"/tokens/smart_money_trades/sol/{token_address}"
        ) or []

    async def wallet_holdings(self, wallet_address: str) -> list[dict]:
        """Current token holdings for a Solana wallet."""
        return await self._get(
            f"/wallet/holdings/sol/{wallet_address}"
        ) or []


gmgn = GMGNClient()
