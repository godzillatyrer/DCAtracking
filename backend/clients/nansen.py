"""Nansen client — Smart Money labels + insider tracking.

Paid API (~$150/mo Standard tier at nansen.ai). If NANSEN_API_KEY is
unset, every method returns None/[] and dependent modules log-and-skip.

Nansen API surface (per docs.nansen.ai as of 2026):
  - Base URL:  https://api.nansen.ai/api/v1
               (the /api/beta namespace was deprecated 2025-10-01)
  - Auth:      `apikey` header (lowercase) with the raw key value
  - Transport: **POST with JSON body** for every data endpoint.
               GETs return 404 because that method is not routed —
               this is why earlier GET probes universally 404'd.

Docs:
  https://docs.nansen.ai/getting-started/api-structure-and-base-url
  https://docs.nansen.ai/getting-started/first-api-call

Rate limits: 20 req/s, 500 req/min (well above our usage).
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

    def _headers(self) -> dict:
        return {
            "apikey": self.key,        # lowercase per docs
            "Content-Type": "application/json",
            "accept": "application/json",
        }

    async def _post(self, path: str, body: dict | None = None) -> Any | None:
        """POST to a Nansen endpoint. Returns parsed JSON or None on error."""
        if not self.configured:
            return None
        url = f"{self.base}{path}"
        try:
            async with httpx.AsyncClient(timeout=25) as client:
                resp = await client.post(url, headers=self._headers(), json=body or {})
                await asyncio.sleep(0.05)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (401, 403):
                    logger.warning(f"Nansen {path} -> {resp.status_code} (auth/plan)")
                elif resp.status_code == 429:
                    logger.warning(f"Nansen {path} -> 429 (rate limit)")
                else:
                    logger.warning(f"Nansen {path} -> {resp.status_code}: {resp.text[:120]}")
        except Exception as e:
            logger.error(f"Nansen error on {path}: {e}")
        return None

    # ─── Public endpoints we actually use ──────────────────────────────
    #
    # All endpoints use POST with a JSON body and live under /api/v1.
    # Keep the surface minimal — add more only when a detection module
    # actually needs them, so we don't burn credits on unused calls.

    async def smart_money_holdings(
        self, chains: list[str] | None = None
    ) -> list[dict]:
        """
        Aggregated smart-money holdings across the specified chains.
        Default: ethereum. Backing endpoint has 'free credit cost' in
        current tier. Returns a list of holding records.
        """
        data = await self._post(
            "/smart-money/holdings",
            {"chains": chains or ["ethereum"]},
        )
        if isinstance(data, dict):
            return data.get("data") or []
        return data or []

    async def smart_money_netflow(
        self,
        chains: list[str] | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> list[dict]:
        """Smart money net inflows vs outflows per token."""
        data = await self._post(
            "/smart-money/netflow",
            {
                "chains": chains or ["ethereum"],
                "pagination": {"page": page, "per_page": per_page},
            },
        )
        if isinstance(data, dict):
            return data.get("data") or []
        return data or []

    async def profiler_address_balances(
        self, addresses: list[str], chains: list[str] | None = None
    ) -> dict | None:
        """Current token balances for one or more wallet addresses.

        Used by portfolio_gate (Module 8) to decide whether a deployer
        has >$1M in holdings.
        """
        return await self._post(
            "/profiler/address/balances",
            {
                "addresses": addresses,
                "chains": chains or ["ethereum"],
            },
        )

    async def profiler_related_wallets(
        self, address: str, chains: list[str] | None = None
    ) -> dict | None:
        """Related-wallet graph for a single address (1 credit/call)."""
        return await self._post(
            "/profiler/address/related-wallets",
            {
                "address": address,
                "chains": chains or ["ethereum"],
            },
        )

    async def profiler_pnl_summary(
        self, address: str, chains: list[str] | None = None
    ) -> dict | None:
        """Aggregate realized PnL for a wallet (1 credit/call)."""
        return await self._post(
            "/profiler/address/pnl-summary",
            {
                "address": address,
                "chains": chains or ["ethereum"],
            },
        )


nansen = NansenClient()
