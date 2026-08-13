"""Vanity-suffix mint watch — the fastest possible CA capture.

Stonk Launcher tokens are CREATE2-mined to end in a fixed suffix (`666666`
by default, confirmed on STONKS / STONKCAT / BROKE). A random address ends
that way roughly once in 16.7 million, so the suffix alone is a sharp
filter — which is what makes this viable where the old chain-wide mint
watch was not: that one alerted on every token on the chain and drowned in
unrelated memecoins.

The trigger is a mint (`Transfer` from the zero address), which is the
first event a token emits, so this fires at the instant the token exists —
ahead of the launcher API and ahead of any indexer.

Runs on its own thread at its own fast cadence: one topic-filtered
`eth_getLogs` per cycle, with the suffix applied client-side before any
metadata lookup, so a fast poll costs one RPC call and nothing else.
"""
import logging
from typing import Any, Dict, List, Optional

import requests

from . import config
from .alerts import (AlertPipeline, ErrorReporter, format_clockin_alert,
                     is_target_token)
from .blockscout import BlockscoutClient
from .rpc import RpcClient, RpcError
from .state import State

log = logging.getLogger("vanity_watcher")


class VanityWatcher:
    name = "vanity_watcher"

    def __init__(self, state: State, pipeline: AlertPipeline,
                 errors: ErrorReporter, rpc: Optional[RpcClient] = None,
                 blockscout: Optional[BlockscoutClient] = None) -> None:
        self.state = state
        self.pipeline = pipeline
        self.errors = errors
        self.rpc = rpc or RpcClient()
        self.blockscout = blockscout or BlockscoutClient()
        self.rpc_down_alerted = False

    def run_once(self) -> None:
        if not config.VANITY_WATCH or not config.VANITY_SUFFIX:
            return
        try:
            head = self.rpc.block_number()
        except RpcError as exc:
            if (self.rpc.consecutive_failures >= config.FAILURE_ALERT_THRESHOLD
                    and not self.rpc_down_alerted):
                self.rpc_down_alerted = True
                self.errors.report(
                    self.name,
                    f"RPC DOWN — vanity mint watch is blind: {exc}",
                    key="vanity_rpc_down")
            return
        if self.rpc_down_alerted:
            self.rpc_down_alerted = False
            self.errors.recovered(self.name, "RPC responding again")

        cursor = self.state.data.get("vanity_last_scanned")
        if not isinstance(cursor, int):
            # Arm at head. Tokens that already exist are not news, and the
            # three from the test window would otherwise all fire at once.
            self.state.data["vanity_last_scanned"] = head
            self.state.mark_dirty()
            self.state.save_if_dirty()
            log.info("vanity mint watch armed at block %d (suffix %s)",
                     head, config.VANITY_SUFFIX)
            return
        if cursor >= head:
            return
        # Never let a long stall turn into an enormous range.
        from_block = max(cursor + 1, head - config.MAX_LOG_WINDOW + 1)

        try:
            logs = self.rpc.get_logs_chunked(
                None, from_block, head,
                topics=[config.TRANSFER_TOPIC0, config.ZERO_TOPIC])
        except RpcError as exc:
            log.warning("vanity mint scan failed: %s", exc)
            return  # cursor holds; the window is retried
        self.state.data["vanity_last_scanned"] = head
        self.state.mark_dirty()

        seen = self.state.data["vanity_seen"]
        for entry in logs:
            addr = (entry.get("address") or "").lower()
            if not addr.endswith(config.VANITY_SUFFIX):
                continue  # the filter that makes this cheap and quiet
            if addr in seen or addr in config.BORING_ADDRESSES:
                continue
            seen.append(addr)
            del seen[:-500]
            self.state.mark_dirty()
            self._alert(addr, entry)
        self.state.save_if_dirty()

    def _alert(self, ca: str, entry: Dict[str, Any]) -> None:
        """Alert immediately; enrich only with what is already available.

        Metadata is best-effort on purpose. A brand-new contract is often
        not indexed yet, and the contract address is the point — waiting for
        a name would trade away the speed this whole component exists for.
        """
        tx_link = config.BLOCKSCOUT_TX_URL.format(
            tx=entry.get("transactionHash", "?"))
        name = symbol = supply = "?"
        provenance = "not verified yet"
        try:
            info = self.blockscout.classify_address(ca)
            token = info.get("token") or {}
            name = str(token.get("name") or "?")
            symbol = str(token.get("symbol") or "?")
            supply = str(token.get("total_supply")
                         or token.get("totalSupply") or "?")
        except requests.RequestException:
            pass
        try:
            creation = self.blockscout.creation_info(ca)
            creator = (creation.get("creator") or "").lower()
            called = (creation.get("called_contract") or "").lower()
            armed = set(self.state.candidate_factories())
            if ({creator, called} & (config.STONK_LAUNCHPAD_CONTRACTS | armed)):
                provenance = "FROM THE STONK LAUNCHPAD"
                self.state.add_tracked(ca, source="chain", name=name,
                                       symbol=symbol)
            elif creator or called:
                provenance = (f"NOT from the known launchpad "
                              f"(deployed by {creator or called})")
        except requests.RequestException:
            pass

        if is_target_token(name, symbol):
            text = format_clockin_alert(ca, "chain (vanity mint)", name,
                                        symbol, supply, tx_link=tx_link)
            self.pipeline.send(text, level="loud", code_lines=[ca],
                               category="launch")
            return
        self.pipeline.send(
            f"{ca}\n"
            f"VANITY TOKEN MINTED — ...{config.VANITY_SUFFIX}\n"
            f"{ca}\n"
            f"{name} ({symbol}) | supply: {supply}\n"
            f"provenance: {provenance}\n"
            f"token: {config.BLOCKSCOUT_TOKEN_URL.format(ca=ca)}\n"
            f"tx:    {tx_link}",
            level="loud", code_lines=[ca], category="launch")

    def interval(self, now=None) -> float:
        return float(config.VANITY_POLL_INTERVAL)
