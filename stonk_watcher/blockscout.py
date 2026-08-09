"""Robinhood Chain Blockscout REST (v2) helpers."""
import logging
from typing import Any, Dict, List, Optional

import requests

from . import config

log = logging.getLogger("blockscout")


class BlockscoutClient:
    def __init__(self, base: Optional[str] = None) -> None:
        self.base = (base or config.BLOCKSCOUT_BASE).rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": config.BROWSER_USER_AGENT,
                                     "Accept": "application/json"})

    def _get(self, path: str) -> Optional[Dict[str, Any]]:
        """GET a v2 endpoint. Returns the JSON dict, None on 404, raises on
        transport errors so callers can count failures."""
        url = f"{self.base}/api/v2/{path.lstrip('/')}"
        resp = self.session.get(url, timeout=20)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            return None

    def address_info(self, address: str) -> Optional[Dict[str, Any]]:
        return self._get(f"addresses/{address}")

    def token_info(self, address: str) -> Optional[Dict[str, Any]]:
        return self._get(f"tokens/{address}")

    def address_transactions(self, address: str,
                             direction: str = "from") -> List[Dict[str, Any]]:
        data = self._get(f"addresses/{address}/transactions?filter={direction}")
        if not data:
            return []
        return data.get("items", []) or []

    def creation_info(self, address: str) -> Dict[str, Any]:
        """Who deployed this contract, and in which block.

        For a launchpad token the creator is the launcher factory itself,
        which is how the factory address is learned without knowing it in
        advance. Returns {creator, block} with None values when unavailable.
        """
        info = self.address_info(address)
        if not info:
            return {"creator": None, "block": None}
        creator = info.get("creator_address_hash") or info.get("creator_address")
        if isinstance(creator, dict):
            creator = creator.get("hash")
        block = None
        tx_hash = info.get("creation_tx_hash") or info.get("creation_transaction_hash")
        if tx_hash:
            try:
                tx = self._get(f"transactions/{tx_hash}")
                if tx:
                    block = tx_block_number(tx)
            except requests.RequestException:
                block = None
        return {"creator": creator.lower() if isinstance(creator, str) else None,
                "block": block}

    def classify_address(self, address: str) -> Dict[str, Any]:
        """Best-effort classification used by the address-extraction heuristic.

        Returns {exists, is_contract, token(dict|None)}.
        """
        info = self.address_info(address)
        if info is None:
            return {"exists": False, "is_contract": False, "token": None}
        token = info.get("token")
        is_contract = bool(info.get("is_contract"))
        if is_contract and not token:
            # Some Blockscout versions only expose token metadata on /tokens.
            try:
                token = self.token_info(address)
            except requests.RequestException:
                token = None
        return {"exists": True, "is_contract": is_contract, "token": token or None}


def tx_creates_contract(tx: Dict[str, Any]) -> Optional[str]:
    """Return the created contract address if a Blockscout tx item is a
    contract creation, else None."""
    created = tx.get("created_contract") or {}
    if isinstance(created, dict) and created.get("hash"):
        return created["hash"]
    to = tx.get("to")
    if to is None or (isinstance(to, dict) and not to.get("hash")):
        raw = tx.get("raw_input") or tx.get("input")
        if raw and raw != "0x":
            return "(pending-index)"  # creation seen, address not indexed yet
    return None


def tx_block_number(tx: Dict[str, Any]) -> Optional[int]:
    for key in ("block_number", "block"):
        value = tx.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None
