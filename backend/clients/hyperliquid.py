"""Hyperliquid public info-endpoint client.

The `/info` endpoint is fully public, no API key required. Free.
We use:
  - `meta` / `metaAndAssetCtxs`: list of all listed coins + current
    context (price, OI, 24h volume, funding)
  - `recentTrades`: last ~1000 trades for a given coin
  - `userFills`: a wallet's complete fill history (used for the
    "fresh wallet" classification)

Rate-limit posture: HL's documented limits are generous on /info.
The watcher hits ~50–100 coins per cycle at ~1 req each, plus a
handful of userFills lookups for whale candidates. Stays well
under any reasonable cap.
"""

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api.hyperliquid.xyz"


class HyperliquidClient:
    @property
    def configured(self) -> bool:
        return True  # public API

    async def _post_info(self, payload: dict, timeout: float = 15.0) -> Optional[Any]:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(f"{BASE_URL}/info", json=payload)
                if resp.status_code == 200:
                    return resp.json()
                logger.warning(
                    f"Hyperliquid /info {payload.get('type')} -> "
                    f"{resp.status_code}: {resp.text[:200]}"
                )
        except Exception as e:
            logger.error(f"Hyperliquid /info error ({payload.get('type')}): {e}")
        return None

    async def meta_and_ctxs(self) -> tuple[list[dict], list[dict]]:
        """Returns (universe, asset_contexts). Same index alignment.
        asset_contexts has: markPx, oraclePx, openInterest, dayNtlVlm,
        funding, etc."""
        data = await self._post_info({"type": "metaAndAssetCtxs"})
        if isinstance(data, list) and len(data) >= 2:
            universe = (data[0] or {}).get("universe") or []
            ctxs = data[1] or []
            return universe, ctxs
        return [], []

    async def recent_trades(self, coin: str) -> list[dict]:
        """Last ~1000 trades for a coin. Each trade:
            { coin, side: "A"|"B", px, sz, hash, time, tid, users:[addr,addr] }
        """
        data = await self._post_info({"type": "recentTrades", "coin": coin})
        return data or []

    async def user_fill_count(self, address: str) -> int:
        """Total fill count for a wallet — used to classify "fresh."
        HL's userFills returns up to 2000 fills; we just len() it."""
        if not address:
            return 0
        data = await self._post_info({"type": "userFills", "user": address})
        if isinstance(data, list):
            return len(data)
        return 0


hyperliquid = HyperliquidClient()
