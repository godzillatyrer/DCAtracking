"""Track 2 — on-chain watcher.

2a. Dev-wallet layer (list of team wallets): baseline silently on first run,
    alert on outgoing txs, loud alert on contract creations.
2b. Candidate-factory auto-arm: every contract a team wallet deploys is
    scanned topic-agnostically with chunked eth_getLogs; token addresses are
    extracted from indexed topics and confirmed via Blockscout. Once a
    factory produces a confirmed token we learn its topic0 and filter on it.
2c. Cadence escalation is handled by interval() (20s -> 5s in the war room).

Also watches the optional AMM factory for logs referencing tracked CAs
(LP/market detection for the base watcher's registry).
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from . import config
from .alerts import AlertPipeline, ErrorReporter, format_clockin_alert, is_target_token
from .blockscout import BlockscoutClient, tx_block_number, tx_creates_contract
from .rpc import RpcClient, RpcError, topic_to_address
from .state import State

log = logging.getLogger("chain_watcher")

MAX_ACTIVITY_ALERTS_PER_CYCLE = 5


class ChainWatcher:
    name = "chain_watcher"

    def __init__(self, state: State, pipeline: AlertPipeline, errors: ErrorReporter,
                 rpc: Optional[RpcClient] = None,
                 blockscout: Optional[BlockscoutClient] = None) -> None:
        self.state = state
        self.pipeline = pipeline
        self.errors = errors
        self.rpc = rpc or RpcClient()
        self.blockscout = blockscout or BlockscoutClient()
        self.rpc_down_alerted = False
        self.bs_failures = 0
        self.bs_down_alerted = False

    # -- 2a: dev wallets -----------------------------------------------------
    def check_dev_wallets(self) -> None:
        for wallet in config.TEAM_WALLETS:
            wstate = self.state.wallet_state(wallet)
            try:
                txs = self.blockscout.address_transactions(wallet, direction="from")
            except requests.RequestException as exc:
                self._blockscout_failure(exc)
                continue
            self._blockscout_ok()

            if not wstate["baselined"]:
                # First sight of this wallet: record history silently.
                wstate["seen_tx_hashes"] = [t.get("hash") for t in txs if t.get("hash")]
                wstate["baselined"] = True
                self.state.mark_dirty()
                log.info("baselined wallet %s with %d historical txs",
                         wallet, len(wstate["seen_tx_hashes"]))
                continue

            for tx in reversed(txs):  # oldest first
                tx_hash = tx.get("hash")
                if not tx_hash or tx_hash in wstate["seen_tx_hashes"]:
                    continue
                wstate["seen_tx_hashes"].append(tx_hash)
                wstate["seen_tx_hashes"] = wstate["seen_tx_hashes"][-500:]
                self.state.mark_dirty()
                created = tx_creates_contract(tx)
                tx_link = config.BLOCKSCOUT_TX_URL.format(tx=tx_hash)
                if created and created != "(pending-index)":
                    block = tx_block_number(tx) or self._safe_head()
                    self.on_contract_creation(wallet, created, block, tx_link)
                elif created:
                    self.pipeline.send(
                        f"TEAM WALLET CONTRACT CREATION (address not indexed yet)\n"
                        f"wallet: {wallet}\ntx: {tx_link}", level="loud")
                else:
                    to = tx.get("to") or {}
                    to_hash = to.get("hash") if isinstance(to, dict) else to
                    self.pipeline.send(
                        f"TEAM WALLET OUTGOING TX\nwallet: {wallet}\n"
                        f"to: {to_hash}\nmethod: {tx.get('method') or '?'}\n"
                        f"tx: {tx_link}", level="alert")
        self.state.save_if_dirty()

    def on_contract_creation(self, wallet: str, contract: str, block: int,
                             tx_link: str) -> None:
        added = self.state.add_candidate_factory(contract, block)
        self.pipeline.send(
            f"TEAM WALLET DEPLOYED A CONTRACT — possible Stonk Launcher factory\n"
            f"{contract}\nwallet: {wallet}\ndeploy block: {block}\n"
            f"contract: {config.BLOCKSCOUT_ADDRESS_URL.format(addr=contract)}\n"
            f"tx: {tx_link}\n"
            f"{'Now watching ALL logs it emits.' if added else '(already watching)'}",
            level="loud", code_lines=[contract])
        self.state.save_if_dirty()

    # -- 2b: candidate factories ---------------------------------------------
    def scan_candidate_factories(self, head: int) -> None:
        for factory, fstate in list(self.state.candidate_factories().items()):
            from_block = fstate["last_scanned"] + 1
            if from_block > head:
                continue
            topics = [fstate["learned_topic0"]] if fstate["learned_topic0"] else None
            try:
                logs = self.rpc.get_logs_chunked(factory, from_block, head, topics)
            except RpcError as exc:
                log.warning("factory scan failed for %s: %s", factory, exc)
                continue
            fstate["last_scanned"] = head
            self.state.mark_dirty()
            self._process_factory_logs(factory, fstate, logs)
        self.state.save_if_dirty()

    def _process_factory_logs(self, factory: str, fstate: Dict[str, Any],
                              logs: List[Dict[str, Any]]) -> None:
        activity_alerts = 0
        for entry in logs:
            tx_hash = entry.get("transactionHash", "?")
            log_key = f"{tx_hash}:{entry.get('logIndex', '?')}"
            if self.state.factory_log_seen(factory, log_key):
                continue
            self.state.mark_factory_log_seen(factory, log_key)
            topics = entry.get("topics") or []
            topic0 = topics[0] if topics else "(no topic)"
            tx_link = config.BLOCKSCOUT_TX_URL.format(tx=tx_hash)

            if activity_alerts < MAX_ACTIVITY_ALERTS_PER_CYCLE:
                activity_alerts += 1
                self.pipeline.send(
                    f"CANDIDATE FACTORY ACTIVITY\nfactory: {factory}\n"
                    f"topic0: {topic0}\ntx: {tx_link}", level="alert")
            elif activity_alerts == MAX_ACTIVITY_ALERTS_PER_CYCLE:
                activity_alerts += 1
                self.pipeline.send(
                    f"CANDIDATE FACTORY ACTIVITY — more logs this cycle from "
                    f"{factory}; further per-log alerts suppressed (token "
                    f"discoveries still alert individually).", level="info")

            for topic in topics[1:]:
                candidate = topic_to_address(topic)
                if candidate is None:
                    continue
                self._check_candidate_token(factory, fstate, candidate, topic0, tx_link)

    def _check_candidate_token(self, factory: str, fstate: Dict[str, Any],
                               candidate: str, topic0: str, tx_link: str) -> None:
        if self.state.is_tracked(candidate) or candidate in fstate["confirmed_tokens"]:
            return
        try:
            info = self.blockscout.classify_address(candidate)
        except requests.RequestException as exc:
            self._blockscout_failure(exc)
            return
        self._blockscout_ok()
        token = info.get("token")
        if not (info.get("is_contract") and token):
            return

        name = str(token.get("name") or "?")
        symbol = str(token.get("symbol") or "?")
        supply = str(token.get("total_supply") or token.get("totalSupply") or "?")
        fstate["confirmed_tokens"].append(candidate)
        if not fstate["learned_topic0"] and topic0 != "(no topic)":
            # Prefer filtered queries on this topic from now on (cheaper, and
            # future-proofs the watcher for launch #2, #3, ...).
            fstate["learned_topic0"] = topic0
            log.info("learned token-creation topic0 for %s: %s", factory, topic0)
        self.state.add_tracked(candidate, source="chain", name=name, symbol=symbol,
                               factory=factory)
        self.state.mark_dirty()
        self.state.save_if_dirty()

        if is_target_token(name, symbol):
            text = format_clockin_alert(candidate, "chain", name, symbol, supply,
                                        tx_link=tx_link)
            self.pipeline.send(text, level="loud", code_lines=[candidate],
                               category="launch")
        else:
            self.pipeline.send(
                f"NEW TOKEN VIA NEW FACTORY\n{candidate}\n"
                f"{name} ({symbol}) | supply: {supply}\nfactory: {factory}\n"
                f"token: {config.BLOCKSCOUT_TOKEN_URL.format(ca=candidate)}\n"
                f"tx: {tx_link}", level="loud", code_lines=[candidate],
                category="launch")

    # -- LP/market detection on the optional AMM factory ---------------------
    def scan_amm_factory(self, head: int) -> None:
        factory = config.AMM_FACTORY_ADDRESS
        if not factory or not self.state.data["tracked"]:
            return
        last = self.state.data.get("amm_last_scanned")
        from_block = (last + 1) if isinstance(last, int) else max(
            head - 500, config.AMM_FACTORY_START_BLOCK)
        if from_block > head:
            return
        try:
            logs = self.rpc.get_logs_chunked(factory, from_block, head)
        except RpcError as exc:
            log.warning("AMM factory scan failed: %s", exc)
            return
        self.state.data["amm_last_scanned"] = head
        self.state.mark_dirty()
        for entry in logs:
            for topic in (entry.get("topics") or [])[1:]:
                candidate = topic_to_address(topic)
                if candidate and self.state.is_tracked(candidate):
                    info = self.state.data["tracked"][candidate]
                    tx_link = config.BLOCKSCOUT_TX_URL.format(
                        tx=entry.get("transactionHash", "?"))
                    self.pipeline.send(
                        f"LP/MARKET EVENT FOR TRACKED TOKEN\n{candidate}\n"
                        f"{info.get('name', '?')} ({info.get('symbol', '?')})\n"
                        f"factory: {factory}\ntx: {tx_link}",
                        level="loud", code_lines=[candidate])
        self.state.save_if_dirty()

    # -- configured contract watch -------------------------------------------
    def seed_watched_contracts(self, head: int) -> None:
        """Arm any contract listed in WATCH_CONTRACTS, so a factory address
        learned by other means is covered without waiting to see it deployed.
        """
        for addr in config.WATCH_CONTRACTS:
            if addr.lower() in self.state.candidate_factories():
                continue
            start = max(0, head - config.WATCH_CONTRACTS_LOOKBACK)
            self.state.add_candidate_factory(addr, start)
            log.info("armed configured contract %s from block %d", addr, start)
            self.pipeline.send(
                f"NOW WATCHING CONFIGURED CONTRACT\n{addr}\n"
                f"scanning all logs from block {start}.",
                level="info", category="health")
        self.state.save_if_dirty()

    # -- chain-wide mint watch -----------------------------------------------
    def scan_new_mints(self, head: int) -> None:
        """Catch any contract minting a token for the first time, anywhere on
        the chain.

        Everything else on-chain here is conditional: it only sees a token if
        that token came out of a factory we already knew to watch. If the
        launcher factory is deployed by a wallet we never see, or already
        exists, those paths are blind. A mint (Transfer from the zero
        address) is unavoidable — a token cannot be distributed without one —
        so this is the net that does not depend on knowing the factory.
        """
        if not config.MINT_WATCH:
            return
        last = self.state.data.get("mint_last_scanned")
        if not isinstance(last, int):
            # First run: start at the current head. Scanning history would
            # alert on every token that ever launched on this chain.
            self.state.data["mint_last_scanned"] = head
            self.state.mark_dirty()
            self.state.save_if_dirty()
            log.info("mint watch armed at block %d", head)
            return
        if last >= head:
            return

        try:
            logs = self.rpc.get_logs_chunked(
                None, last + 1, head,
                topics=[config.TRANSFER_TOPIC0, config.ZERO_TOPIC])
        except RpcError as exc:
            log.warning("mint scan failed: %s", exc)
            return  # leave the cursor put so the range is retried
        self.state.data["mint_last_scanned"] = head
        self.state.mark_dirty()

        seen = self.state.data["mint_seen_tokens"]
        checked = 0
        for entry in logs:
            addr = (entry.get("address") or "").lower()
            if not addr or addr in seen or addr in config.BORING_ADDRESSES:
                continue
            seen.append(addr)
            self.state.mark_dirty()
            if checked >= config.MAX_MINT_CHECKS_PER_CYCLE:
                log.warning("mint check cap hit; %s deferred", addr)
                continue
            checked += 1
            self._check_minted_token(addr, entry)
        self.state.save_if_dirty()

    def _check_minted_token(self, addr: str, entry: Dict[str, Any]) -> None:
        if self.state.is_tracked(addr):
            return  # another track already alerted on this one
        try:
            info = self.blockscout.classify_address(addr)
        except requests.RequestException as exc:
            self._blockscout_failure(exc)
            return
        self._blockscout_ok()
        token = info.get("token")
        if not token:
            return
        name = str(token.get("name") or "?")
        symbol = str(token.get("symbol") or "?")
        supply = str(token.get("total_supply") or token.get("totalSupply") or "?")
        token_type = str(token.get("type") or "").upper()
        clockin = is_target_token(name, symbol)
        # NFT mints are constant background noise on this chain; only a name
        # match earns an alert from them.
        if "721" in token_type or "1155" in token_type:
            if not clockin:
                return
        tx_link = config.BLOCKSCOUT_TX_URL.format(
            tx=entry.get("transactionHash", "?"))
        self.state.add_tracked(addr, source="mint", name=name, symbol=symbol)
        self.state.mark_dirty()

        if clockin:
            text = format_clockin_alert(addr, "chain (first mint)", name, symbol,
                                        supply, tx_link=tx_link)
            self.pipeline.send(text, level="loud", code_lines=[addr],
                               category="launch")
        else:
            self.pipeline.send(
                f"NEW TOKEN MINTED ON CHAIN\n{addr}\n"
                f"{name} ({symbol}) | supply: {supply}\n"
                f"token: {config.BLOCKSCOUT_TOKEN_URL.format(ca=addr)}\n"
                f"tx: {tx_link}\n"
                "First mint seen on Robinhood Chain — may or may not be from "
                "the launchpad.",
                level="loud", code_lines=[addr], category="launch")

    # -- plumbing ------------------------------------------------------------
    def _safe_head(self) -> int:
        try:
            return self.rpc.block_number()
        except RpcError:
            return 0

    def _blockscout_failure(self, exc: Exception) -> None:
        self.bs_failures += 1
        log.warning("blockscout failure #%d: %s", self.bs_failures, exc)
        if (self.bs_failures >= config.FAILURE_ALERT_THRESHOLD
                and not self.bs_down_alerted):
            self.bs_down_alerted = True
            self.errors.report(
                self.name,
                f"Blockscout not responding ({self.bs_failures} consecutive "
                "failures) — dev-wallet watch and token confirmation degraded.",
                key="blockscout_down")

    def _blockscout_ok(self) -> None:
        self.bs_failures = 0
        if self.bs_down_alerted:
            self.bs_down_alerted = False
            self.errors.recovered(self.name, "Blockscout responding again")

    def run_once(self) -> None:
        try:
            head = self.rpc.block_number()
            if self.rpc_down_alerted:
                self.rpc_down_alerted = False
                self.errors.recovered(self.name, "RPC responding again")
        except RpcError as exc:
            if (self.rpc.consecutive_failures >= config.FAILURE_ALERT_THRESHOLD
                    and not self.rpc_down_alerted):
                self.rpc_down_alerted = True
                self.errors.report(
                    self.name,
                    f"RPC DOWN — {self.rpc.consecutive_failures} consecutive "
                    f"failures on {self.rpc.url}: {exc}. On-chain detection is "
                    "blind until this recovers (API path still live).",
                    key="rpc_down")
            log.warning("no chain head this cycle: %s", exc)
            head = None

        self.check_dev_wallets()
        if head is not None:
            self.seed_watched_contracts(head)
            # Chain-wide net first: it is the path that does not depend on
            # having guessed the factory correctly.
            self.scan_new_mints(head)
            self.scan_candidate_factories(head)
            self.scan_amm_factory(head)

    def interval(self, now: Optional[datetime] = None) -> float:
        now = now or datetime.now(timezone.utc)
        return config.chain_poll_interval(now, config.get_t0())
