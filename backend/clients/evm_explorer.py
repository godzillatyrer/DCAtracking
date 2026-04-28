"""Unified Etherscan + BscScan client.

Used by the EVM wallet classifier to fetch a wallet's recent
transactions (count + first/last timestamps) for fresh/dormant
classification.

Both explorers share the same v1 API shape (`module=account&
action=txlist`); we just swap the base URL and API key per chain.

Free tier on both: 5 req/sec, 100k req/day. Plenty for our use.
"""

import logging
from typing import Any

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)


CHAIN_CONFIG = {
    "eth": {
        "base": "https://api.etherscan.io/api",
        "key_attr": "ETHERSCAN_API_KEY",
    },
    "bsc": {
        "base": "https://api.bscscan.com/api",
        "key_attr": "BSCSCAN_API_KEY",
    },
}


def configured_for(chain: str) -> bool:
    cfg = CHAIN_CONFIG.get(chain)
    if not cfg:
        return False
    return bool(getattr(settings, cfg["key_attr"], None))


def _key(chain: str) -> str:
    cfg = CHAIN_CONFIG.get(chain) or {}
    return getattr(settings, cfg.get("key_attr") or "", "") or ""


async def get_recent_txs(
    address: str, chain: str, limit: int = 50,
) -> list[dict]:
    """Returns the wallet's most-recent N normal transactions, newest
    first. Each row has: hash, blockNumber, timeStamp (unix string),
    from, to, value, isError, ...

    Empty list on any failure (missing key, rate-limit, network, etc).
    """
    cfg = CHAIN_CONFIG.get(chain)
    if not cfg or not address:
        return []
    api_key = _key(chain)
    if not api_key:
        return []
    params = {
        "module": "account",
        "action": "txlist",
        "address": address,
        "startblock": 0,
        "endblock": 99999999,
        "page": 1,
        "offset": limit,
        "sort": "desc",
        "apikey": api_key,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(cfg["base"], params=params)
            if resp.status_code != 200:
                logger.warning(
                    f"{chain} explorer HTTP {resp.status_code} for "
                    f"{address[:8]}: {resp.text[:200]}"
                )
                return []
            data = resp.json()
            # status="1" + result=list — happy path
            # status="0" + message="No transactions found" — empty wallet
            if data.get("status") == "1" and isinstance(data.get("result"), list):
                return data["result"]
            if data.get("status") == "0":
                msg = (data.get("message") or "").lower()
                if "no transactions" in msg:
                    return []
                # Rate limit / NOTOK / etc.
                logger.warning(
                    f"{chain} explorer returned status=0 for "
                    f"{address[:8]}: {data.get('message')}"
                )
                return []
    except Exception as e:
        logger.error(f"{chain} explorer error for {address[:8]}: {e}")
    return []
