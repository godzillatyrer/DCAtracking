"""
Arkham Intelligence API client.

Provides wallet entity lookups — tells us who owns an address.
This is used to:
1. Label known wallets (exchange, VC, market maker, etc.)
2. Filter out false positives (a Binance wallet isn't an insider)
3. Identify actual insider entities
4. Enrich wallet tracker with real entity names

Free tier: 10,000 intel label lookups per billing period.
"""

import logging

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)


async def lookup_address(address: str) -> dict | None:
    """
    Look up an address on Arkham Intelligence.
    Returns entity info if found, None if not labeled or API fails.

    Response includes:
    - arkhamEntity.name — "Binance", "Jump Trading", etc.
    - arkhamEntity.type — "exchange", "fund", "service", etc.
    - arkhamEntity.id — Arkham entity ID
    """
    if not settings.ARKHAM_API_KEY:
        return None

    url = f"{settings.ARKHAM_BASE_URL}/intelligence/address/{address.lower()}"
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(url, headers={"API-Key": settings.ARKHAM_API_KEY})
            if resp.status_code == 200:
                data = resp.json()
                entity = data.get("arkhamEntity") or data.get("entity")
                if entity:
                    return {
                        "name": entity.get("name", ""),
                        "type": entity.get("type", ""),
                        "id": entity.get("id", ""),
                        "website": entity.get("website", ""),
                    }
                # Address exists but is not labeled by Arkham
                return None
            elif resp.status_code == 429:
                logger.warning("Arkham rate limit hit — backing off")
                return None
            elif resp.status_code == 404:
                return None
            else:
                logger.warning(f"Arkham API {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.error(f"Arkham API error: {e}")
    return None


def is_exchange_or_infrastructure(entity: dict) -> bool:
    """Check if an Arkham entity is an exchange, bridge, or infrastructure."""
    if not entity:
        return False
    entity_type = entity.get("type", "").lower()
    name = entity.get("name", "").lower()
    infra_types = {"exchange", "dex", "bridge", "service", "infrastructure"}
    infra_names = {"binance", "gate.io", "bybit", "kucoin", "okx", "mexc", "bitget",
                   "pancakeswap", "uniswap", "1inch", "coinbase"}
    return entity_type in infra_types or any(n in name for n in infra_names)


async def label_wallets(addresses: list[str]) -> dict[str, dict]:
    """
    Batch label multiple addresses via Arkham.
    Returns dict mapping address → entity info.
    Only looks up addresses we haven't labeled before.
    Uses ~1 API credit per unique labeled address returned.
    """
    results = {}
    for addr in addresses:
        entity = await lookup_address(addr)
        if entity:
            results[addr.lower()] = entity
    return results
