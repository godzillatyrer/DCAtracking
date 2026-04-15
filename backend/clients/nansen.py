"""Nansen client — Smart Money labels + insider tracking.

Paid API (pricing varies by tier — see nansen.ai). If NANSEN_API_KEY is
unset, every method returns None/[] and dependent modules log-and-skip.

Nansen API surface (per docs.nansen.ai as of 2026):
  - Base URL:  https://api.nansen.ai/api/v1
               (the /api/beta namespace was deprecated 2025-10-01)
  - Auth:      `apikey` header (lowercase) with the raw key value
  - Transport: **POST with JSON body** for every data endpoint.
               GETs return 404 because that method is not routed.

Credit metering:
  Nansen bills per call (observed: /smart-money/holdings = 5 credits).
  To avoid accidentally draining the user's balance via diagnostic
  probes or a runaway job, THIS CLIENT enforces a hard daily call cap
  (settings.NANSEN_DAILY_CALL_CAP). Once hit, further calls short-
  circuit to None and log one warning. Counter resets at UTC midnight.
  Inspect via `nansen.calls_today` / `nansen.cap_hit`.

Docs:
  https://docs.nansen.ai/getting-started/api-structure-and-base-url
  https://docs.nansen.ai/getting-started/first-api-call

Rate limits: 20 req/s, 500 req/min (well above our usage).
"""

import asyncio
import logging
from datetime import datetime, date
from typing import Any

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)


class NansenClient:
    def __init__(self):
        self.base = settings.NANSEN_BASE_URL.rstrip("/")
        self.key = settings.NANSEN_API_KEY
        # In-process credit-aware call counter.
        self._counter_date: date = datetime.utcnow().date()
        self._calls_today: int = 0
        self._cap_hit_logged_today: bool = False
        # Timestamp of the last successful 200 — lets diagnostics show
        # "last verified X ago" without re-probing on every audit.
        self._last_success_at: datetime | None = None

    @property
    def configured(self) -> bool:
        return bool(self.key)

    def _rollover_counter_if_needed(self) -> None:
        today = datetime.utcnow().date()
        if today != self._counter_date:
            self._counter_date = today
            self._calls_today = 0
            self._cap_hit_logged_today = False

    @property
    def calls_today(self) -> int:
        self._rollover_counter_if_needed()
        return self._calls_today

    @property
    def cap_hit(self) -> bool:
        self._rollover_counter_if_needed()
        return self._calls_today >= settings.NANSEN_DAILY_CALL_CAP

    @property
    def last_success_at(self) -> datetime | None:
        return self._last_success_at

    def _headers(self) -> dict:
        return {
            "apikey": self.key,        # lowercase per docs
            "Content-Type": "application/json",
            "accept": "application/json",
        }

    async def _post(self, path: str, body: dict | None = None) -> Any | None:
        """POST to a Nansen endpoint. Returns parsed JSON or None on error.

        Enforces NANSEN_DAILY_CALL_CAP — calls past the cap are skipped
        with a single warning per day. This is the primary guard against
        credit-balance drain from bugs or accidentally-aggressive jobs.
        """
        if not self.configured:
            return None
        self._rollover_counter_if_needed()
        if self._calls_today >= settings.NANSEN_DAILY_CALL_CAP:
            if not self._cap_hit_logged_today:
                logger.warning(
                    f"Nansen daily call cap ({settings.NANSEN_DAILY_CALL_CAP}) "
                    f"reached — skipping further calls until UTC midnight. "
                    f"Tune via NANSEN_DAILY_CALL_CAP env."
                )
                self._cap_hit_logged_today = True
            return None

        url = f"{self.base}{path}"
        # Increment BEFORE the request so retries / concurrent calls
        # don't blow past the cap under load.
        self._calls_today += 1

        try:
            async with httpx.AsyncClient(timeout=25) as client:
                resp = await client.post(url, headers=self._headers(), json=body or {})
                await asyncio.sleep(0.05)
                if resp.status_code == 200:
                    self._last_success_at = datetime.utcnow()
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
