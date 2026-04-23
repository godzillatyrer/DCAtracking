"""DexScreener token-info helper — cheap liquidity / MC / volume lookup.

Used by the alert dispatcher (MC filter) and the convergence API
(cluster card enrichment). Free, no key required. Cached in-process
for ~2 min to avoid hammering the API on bursty requests.
"""

import time
from typing import Optional

import httpx

from backend.config import settings

_CACHE: dict[str, tuple[float, Optional[dict]]] = {}
_TTL = 120.0


async def token_info(mint: str) -> Optional[dict]:
    """Return the best-liquidity pair's info for a Solana mint.

    Returns dict with: price_usd, liquidity_usd, market_cap_usd,
    volume_24h_usd, pair_url, symbol, name. None on any failure.
    """
    entry = _CACHE.get(mint)
    if entry and time.time() - entry[0] < _TTL:
        return entry[1]
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                f"{settings.DEXSCREENER_BASE_URL}/latest/dex/tokens/{mint}"
            )
            if resp.status_code != 200:
                _CACHE[mint] = (time.time(), None)
                return None
            pairs = (resp.json() or {}).get("pairs") or []
            if not pairs:
                _CACHE[mint] = (time.time(), None)
                return None
            best = max(
                pairs,
                key=lambda p: float(((p.get("liquidity") or {}).get("usd")) or 0),
            )
            info = {
                "price_usd": float(best.get("priceUsd") or 0),
                "liquidity_usd": float(((best.get("liquidity") or {}).get("usd")) or 0),
                "market_cap_usd": float(best.get("marketCap") or best.get("fdv") or 0),
                "volume_24h_usd": float(((best.get("volume") or {}).get("h24")) or 0),
                "pair_url": best.get("url"),
                "symbol": (best.get("baseToken") or {}).get("symbol"),
                "name": (best.get("baseToken") or {}).get("name"),
            }
            _CACHE[mint] = (time.time(), info)
            return info
    except Exception:
        _CACHE[mint] = (time.time(), None)
        return None
