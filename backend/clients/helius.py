"""Helius client — Solana RPC.

Free tier (100k credits/day) is sufficient for Phase 1. Sign up at
helius.dev. If HELIUS_API_KEY is unset, every method returns None/[]
and the Solana-aware modules log-and-skip.

This client exposes just the endpoints we need:
  - getTokenAccountsByOwner  — portfolio lookups
  - getSignaturesForAddress  — activity feed
  - getTransaction           — parse individual tx
  - Pump.fun / Raydium       — via getProgramAccounts

We use the JSON-RPC endpoint; the enhanced REST API is an easy add later.
"""

import asyncio
import logging
from typing import Any

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)


class HeliusClient:
    def __init__(self):
        self.base = settings.HELIUS_BASE_URL.rstrip("/")
        self.key = settings.HELIUS_API_KEY

    @property
    def configured(self) -> bool:
        return bool(self.key)

    def _url(self) -> str:
        return f"{self.base}/?api-key={self.key}"

    async def _rpc(self, method: str, params: list) -> Any | None:
        if not self.configured:
            return None
        try:
            async with httpx.AsyncClient(timeout=25) as client:
                resp = await client.post(
                    self._url(),
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
                await asyncio.sleep(0.05)
                if resp.status_code == 200:
                    data = resp.json()
                    if "error" in data:
                        logger.warning(f"Helius RPC {method}: {data['error']}")
                        return None
                    return data.get("result")
                logger.warning(f"Helius HTTP {resp.status_code}: {resp.text[:120]}")
        except Exception as e:
            logger.error(f"Helius {method} error: {e}")
        return None

    async def get_slot(self) -> int | None:
        r = await self._rpc("getSlot", [])
        return int(r) if r is not None else None

    async def get_signatures(self, address: str, limit: int = 50) -> list[dict]:
        r = await self._rpc("getSignaturesForAddress", [address, {"limit": limit}])
        return r or []

    async def get_transaction(self, signature: str) -> dict | None:
        return await self._rpc(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        )

    async def get_balance(self, address: str) -> int | None:
        r = await self._rpc("getBalance", [address])
        if r and isinstance(r, dict):
            return int(r.get("value", 0))
        return None

    async def get_token_accounts_by_owner(self, owner: str) -> list[dict]:
        r = await self._rpc(
            "getTokenAccountsByOwner",
            [owner, {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
             {"encoding": "jsonParsed"}],
        )
        if r and isinstance(r, dict):
            return r.get("value") or []
        return []


helius = HeliusClient()
