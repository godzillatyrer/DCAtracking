"""Minimal JSON-RPC client for Robinhood Chain plus the chunked eth_getLogs
helper shared by the factory watchers."""
import logging
import random
import time
from typing import Any, Dict, List, Optional

import requests

from . import config

log = logging.getLogger("rpc")


class RpcError(Exception):
    pass


class RpcClient:
    def __init__(self, url: Optional[str] = None) -> None:
        self.url = url or config.RPC_URL
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json",
                                     "User-Agent": config.BROWSER_USER_AGENT})
        self.consecutive_failures = 0

    def call(self, method: str, params: Optional[list] = None, retries: int = 3) -> Any:
        payload = {"jsonrpc": "2.0", "id": random.randint(1, 10**9),
                   "method": method, "params": params or []}
        last_err: Exception = RpcError("no attempt made")
        for attempt in range(retries):
            try:
                resp = self.session.post(self.url, json=payload, timeout=20)
                if resp.status_code != 200:
                    raise RpcError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                body = resp.json()
                if "error" in body and body["error"]:
                    raise RpcError(str(body["error"])[:300])
                self.consecutive_failures = 0
                return body.get("result")
            except (requests.RequestException, ValueError, RpcError) as exc:
                last_err = exc
                time.sleep(0.5 * (attempt + 1))
        self.consecutive_failures += 1
        raise RpcError(f"{method} failed after {retries} tries: {last_err}")

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber"), 16)

    def get_code(self, address: str) -> str:
        return self.call("eth_getCode", [address, "latest"]) or "0x"

    def get_logs(self, address: Optional[str], from_block: int, to_block: int,
                 topics: Optional[list] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
        }
        if address:
            params["address"] = address
        if topics:
            params["topics"] = topics
        result = self.call("eth_getLogs", [params])
        return result or []

    def get_logs_chunked(self, address: Optional[str], from_block: int,
                         to_block: int, topics: Optional[list] = None,
                         chunk: Optional[int] = None) -> List[Dict[str, Any]]:
        """Walk [from_block, to_block] in chunks, halving the chunk size when
        the node rejects a range as too large. Raises RpcError only if a
        minimal (1-block) request still fails."""
        chunk = chunk or config.LOG_CHUNK_SIZE
        logs: List[Dict[str, Any]] = []
        start = from_block
        while start <= to_block:
            end = min(start + chunk - 1, to_block)
            try:
                logs.extend(self.get_logs(address, start, end, topics))
                start = end + 1
            except RpcError:
                if chunk <= 1:
                    raise
                chunk = max(1, chunk // 2)
                log.warning("getLogs range rejected, retrying with chunk=%d", chunk)
        return logs


def topic_to_address(topic: str) -> Optional[str]:
    """Extract an address from a 32-byte indexed topic if (and only if) it is
    zero-padded like an address: 0x + 24 zeros + 40 hex chars."""
    if not isinstance(topic, str):
        return None
    t = topic.lower()
    if not (t.startswith("0x") and len(t) == 66):
        return None
    if not t[2:26] == "0" * 24:
        return None
    candidate = t[26:]
    if candidate == "0" * 40:
        return None
    try:
        int(candidate, 16)
    except ValueError:
        return None
    return "0x" + candidate
