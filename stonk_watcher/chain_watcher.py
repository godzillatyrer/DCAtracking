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
# Blockscout lookups for creator resolution, per cycle. Bounded so a burst
# of API listings cannot delay the factory log scan that detects launches.
MAX_CREATOR_LOOKUPS_PER_CYCLE = 8
# Retries for a candidate whose creation Blockscout has not yet indexed.
PENDING_MAX_ATTEMPTS = 20


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
        added = self.state.add_candidate_factory(contract, block,
                                                 source="team_wallet")
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
            self._retry_pending(factory, fstate)
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

    def _retry_pending(self, factory: str, fstate: Dict[str, Any]) -> None:
        """Re-check candidates whose creation Blockscout had not yet indexed."""
        for candidate, info in list(fstate.get("pending", {}).items()):
            self._check_candidate_token(factory, fstate, candidate,
                                        info.get("topic0", "(no topic)"),
                                        info.get("tx_link", "?"))
            still = fstate.get("pending", {}).get(candidate)
            if still and still.get("attempts", 0) >= PENDING_MAX_ATTEMPTS:
                fstate["pending"].pop(candidate, None)
                self.state.mark_dirty()
                log.warning("giving up on %s after %d indexing retries",
                            candidate, PENDING_MAX_ATTEMPTS)

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

        # A factory's logs REFERENCE many token addresses it did not create:
        # the quote asset (USDG), LP pairs (themselves ERC-20s), fee or
        # referral tokens. Alerting on every indexed address that resolves to
        # a token is how unrelated tokens reach the phone. Require that this
        # factory actually deployed it.
        try:
            creation = self.blockscout.creation_info(candidate)
        except requests.RequestException as exc:
            self._blockscout_failure(exc)
            return
        creator = (creation.get("creator") or "").lower()
        if creator and creator != factory.lower():
            log.info("skipping %s: referenced by %s but created by %s",
                     candidate, factory, creator)
            return
        if not creator:
            # Blockscout has not indexed the creation yet. The log itself is
            # already marked seen, so queue an explicit retry — otherwise a
            # real launch caught during indexing lag is lost forever.
            pending = fstate.setdefault("pending", {})
            attempts = pending.get(candidate, {}).get("attempts", 0)
            if attempts < PENDING_MAX_ATTEMPTS:
                pending[candidate] = {"topic0": topic0, "tx_link": tx_link,
                                      "attempts": attempts + 1}
                self.state.mark_dirty()
                log.info("creation of %s not indexed yet; queued retry %d/%d",
                         candidate, attempts + 1, PENDING_MAX_ATTEMPTS)
            return
        fstate.get("pending", {}).pop(candidate, None)

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
            self.state.add_candidate_factory(addr, start, source="config")
            log.info("armed configured contract %s from block %d", addr, start)
            self.pipeline.send(
                f"NOW WATCHING CONFIGURED CONTRACT\n{addr}\n"
                f"scanning all logs from block {start}.",
                level="info", category="health")
        self.state.save_if_dirty()

    # -- learn the real factory from a confirmed launchpad token -------------
    def learn_factories_from_tracked(self, head: int) -> None:
        """Resolve the creator of every confirmed launchpad token and arm it.

        A token that appeared in the launcher API is, by definition, from the
        launchpad — so whatever contract deployed it *is* the Stonk Launcher
        factory. Learning it this way needs no guesswork about which wallet
        deployed the factory, and once armed every later launch is caught
        from the factory's own logs, ahead of the API.
        """
        resolved_this_cycle = 0
        for ca, info in list(self.state.data["tracked"].items()):
            # Provenance gate: ONLY a token the launcher API itself listed
            # proves its creator is the launcher factory. Tokens tracked from
            # any other source (chain scans, historical state) must never
            # teach us a factory — that is how generic memecoin factories
            # ended up armed and spamming.
            if info.get("source") != "api":
                continue
            if info.get("creator_resolved"):
                continue
            # Each token costs 2-4 sequential Blockscout calls. Unbounded,
            # a burst of listings at T0 would delay scan_candidate_factories
            # — the actual launch-detecting scan — by minutes.
            if resolved_this_cycle >= MAX_CREATOR_LOOKUPS_PER_CYCLE:
                log.info("creator-lookup cap reached; remaining tokens next cycle")
                break
            resolved_this_cycle += 1
            try:
                creation = self.blockscout.creation_info(ca)
            except requests.RequestException as exc:
                self._blockscout_failure(exc)
                continue  # transient: retry next cycle, stay unresolved

            creator = creation.get("creator")
            if not creator:
                # Indexing lag right after launch. Do NOT mark resolved, or
                # the flagship token would never teach us the factory.
                log.info("creator of %s not indexed yet; retrying next cycle", ca)
                continue

            self._blockscout_ok()
            if creator in config.BORING_ADDRESSES:
                self.state.set_tracked_field(ca, "creator_resolved", True)
                continue
            if creator.lower() in self.state.candidate_factories():
                self.state.set_tracked_field(ca, "creator_resolved", True)
                continue
            # An EOA-deployed token was launched by hand, not by a factory.
            try:
                creator_info = self.blockscout.classify_address(creator)
            except requests.RequestException as exc:
                self._blockscout_failure(exc)
                continue  # transient: leave unresolved so it retries
            self.state.set_tracked_field(ca, "creator_resolved", True)
            if not creator_info.get("is_contract"):
                log.info("token %s was deployed by an EOA, not a factory", ca)
                continue

            block = creation.get("block")
            if not isinstance(block, int):
                block = max(0, head - config.WATCH_CONTRACTS_LOOKBACK)
            self.state.add_candidate_factory(creator, block,
                                             source="learned_from_api")
            log.info("learned launcher factory %s from token %s", creator, ca)
            self.pipeline.send(
                f"LEARNED LAUNCHER FACTORY\n{creator}\n"
                f"deduced from launchpad token {info.get('symbol', ca)}.\n"
                f"contract: {config.BLOCKSCOUT_ADDRESS_URL.format(addr=creator)}\n"
                "Every further launch from this factory is now caught on "
                "chain, ahead of the site.",
                level="info", category="health", code_lines=[creator])
        self.state.save_if_dirty()

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
            self.learn_factories_from_tracked(head)
            self.scan_candidate_factories(head)
            self.scan_amm_factory(head)

    def interval(self, now: Optional[datetime] = None) -> float:
        now = now or datetime.now(timezone.utc)
        return config.chain_poll_interval(now, config.get_t0())
