"""DeFi Llama client — TVL, protocols, token prices, historical mcap.

Free, no key required. Uses two hosts:
  - api.llama.fi       (protocols, TVL, raises, hacks)
  - coins.llama.fi     (token prices, historical)

Rate limits are generous but we still add light client-side throttling.
"""

import asyncio
import logging
from typing import Any

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)


class DeFiLlamaClient:
    """Async wrapper around DeFi Llama public endpoints."""

    def __init__(self):
        self.base = settings.DEFILLAMA_BASE_URL.rstrip("/")
        self.coins = settings.DEFILLAMA_COINS_URL.rstrip("/")

    @property
    def configured(self) -> bool:
        return True  # public API, always configured

    async def _get(self, url: str, timeout: int = 20) -> Any | None:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return resp.json()
                logger.warning(f"DeFiLlama {url} -> {resp.status_code}")
        except Exception as e:
            logger.error(f"DeFiLlama error on {url}: {e}")
        await asyncio.sleep(0.1)
        return None

    # --- Protocols / TVL ---

    async def protocols(self) -> list[dict]:
        """All protocols with current TVL. Used by exploit watcher."""
        data = await self._get(f"{self.base}/protocols")
        return data or []

    async def protocol_detail(self, slug: str) -> dict | None:
        """Full protocol detail including historicalChainTvls."""
        return await self._get(f"{self.base}/protocol/{slug}")

    async def hacks(self) -> list[dict]:
        """
        Historical + recent hacks DB. Each entry typically includes:
          name, date (unix ts), amount (in USD), chain, classification,
          technique, bridgeHack, targetType, source, returnedFunds, etc.

        Used by exploit_watcher to surface real-time exploit alerts
        within minutes — DeFi Llama's security team adds entries
        promptly after a hack is publicly disclosed.
        """
        return await self._get(f"{self.base}/hacks") or []

    # --- Token historicals ---

    async def token_price_chart(
        self, chain: str, address: str, period: str = "7d"
    ) -> list[tuple[int, float]]:
        """
        Historical price chart for a token.
        chain: 'bsc', 'ethereum', 'solana', etc.
        Returns list of (unix_ts, price_usd) pairs.
        """
        key = f"{chain}:{address}"
        data = await self._get(
            f"{self.coins}/chart/{key}?span=168&period={period}"
        )
        if not data:
            return []
        prices = data.get("coins", {}).get(key, {}).get("prices", [])
        return [(int(p["timestamp"]), float(p["price"])) for p in prices]

    async def current_prices(self, chain_address_pairs: list[str]) -> dict[str, dict]:
        """
        Current USD prices for a batch of tokens.
        Input: ['bsc:0xabc', 'solana:So111...', ...]
        Returns: { 'bsc:0xabc': {price, mcap, ...}, ... }
        """
        if not chain_address_pairs:
            return {}
        key = ",".join(chain_address_pairs)
        data = await self._get(f"{self.coins}/prices/current/{key}")
        return (data or {}).get("coins", {})


defillama = DeFiLlamaClient()
