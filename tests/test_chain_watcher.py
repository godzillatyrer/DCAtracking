"""Acceptance test 2: a team-wallet contract creation enters
`candidate_factories` and the topic-agnostic log scan runs. Also covers the
address-extraction heuristic (the offline version of acceptance test 3),
learned-topic0 behavior, and restart survival for the chain path.
"""
from stonk_watcher import config
from stonk_watcher.chain_watcher import ChainWatcher
from stonk_watcher.rpc import topic_to_address
from stonk_watcher.state import State

WALLET = config.TEAM_WALLETS[1]  # 0xBe49... (the new one)
FACTORY = "0xfac70fac70fac70fac70fac70fac70fac70fac70"
TOKEN = "0x3333333333333333333333333333333333333333"
TOPIC_MARKET_CREATED = "0x" + "ab" * 32


def pad_address(addr):
    return "0x" + "0" * 24 + addr[2:].lower()


class FakeRpc:
    def __init__(self, logs_by_address=None, head=1000):
        self.logs_by_address = logs_by_address or {}
        self.head = head
        self.get_logs_calls = []
        self.consecutive_failures = 0
        self.url = "fake://rpc"

    def block_number(self):
        return self.head

    def get_logs_chunked(self, address, from_block, to_block, topics=None, chunk=None):
        self.get_logs_calls.append(
            {"address": address, "from": from_block, "to": to_block, "topics": topics})
        return self.logs_by_address.get((address or "").lower(), [])


class FakeBlockscout:
    def __init__(self, txs_by_wallet=None, tokens=None, creators=None):
        self.txs_by_wallet = txs_by_wallet or {}
        self.tokens = tokens or {}
        self.creators = creators or {}

    def address_transactions(self, address, direction="from"):
        return self.txs_by_wallet.get(address.lower(), [])

    def creation_info(self, address):
        return self.creators.get(address.lower(), {"creator": None, "block": None})

    def classify_address(self, address):
        token = self.tokens.get(address.lower())
        if token is not None:
            return {"exists": True, "is_contract": True, "token": token}
        return {"exists": False, "is_contract": False, "token": None}


def creation_tx(tx_hash, contract, block=900):
    return {"hash": tx_hash, "to": None,
            "created_contract": {"hash": contract}, "block_number": block}


def factory_log(tx_hash, topics, log_index=0):
    return {"transactionHash": tx_hash, "logIndex": hex(log_index),
            "topics": topics}


def make_watcher(state, pipeline, errors, rpc=None, blockscout=None):
    return ChainWatcher(state, pipeline, errors,
                        rpc=rpc or FakeRpc(),
                        blockscout=blockscout or FakeBlockscout())


def test_contract_creation_enters_candidate_factories(state, pipeline, errors):
    bs = FakeBlockscout(txs_by_wallet={WALLET.lower(): []})
    watcher = make_watcher(state, pipeline, errors, blockscout=bs)
    watcher.run_once()  # baseline pass: wallet history is recorded silently
    assert not pipeline.sent, "baselining must be silent"

    bs.txs_by_wallet[WALLET.lower()] = [creation_tx("0xdead1", FACTORY)]
    watcher.run_once()

    assert FACTORY.lower() in state.candidate_factories()
    assert state.candidate_factories()[FACTORY.lower()]["deploy_block"] == 900
    # Still detected and armed internally, but categorised as recon so it
    # does not reach the phone in the default launches-only mode.
    deploy = pipeline.find("TEAM WALLET DEPLOYED A CONTRACT")
    assert deploy and deploy[0]["category"] == "recon"

    # Topic-agnostic scan ran against the new factory (no topic filter).
    scans = [c for c in watcher.rpc.get_logs_calls
             if (c["address"] or "").lower() == FACTORY.lower()]
    assert scans and scans[0]["topics"] is None
    assert scans[0]["from"] == 900, "scan starts at the deploy block"


def test_token_discovery_from_raw_logs(state, pipeline, errors):
    """Address-extraction heuristic end to end: raw factory logs -> token
    confirmed via Blockscout -> NEW TOKEN VIA NEW FACTORY -> tracked."""
    state.add_candidate_factory(FACTORY, 900)
    rpc = FakeRpc(logs_by_address={FACTORY.lower(): [
        factory_log("0xaaa1", [TOPIC_MARKET_CREATED, pad_address(TOKEN),
                               "0x" + "11" * 32]),  # second topic is not an address
    ]})
    bs = FakeBlockscout(tokens={TOKEN: {"name": "MANCER", "symbol": "MANCER",
                                        "total_supply": "42"}})
    watcher = make_watcher(state, pipeline, errors, rpc=rpc, blockscout=bs)
    watcher.run_once()

    assert pipeline.find("CANDIDATE FACTORY ACTIVITY")
    discovery = pipeline.find("NEW TOKEN VIA NEW FACTORY")
    assert discovery and discovery[0]["level"] == "loud"
    assert discovery[0]["category"] == "launch", "must survive the filter"
    assert "MANCER" in discovery[0]["text"]
    # Supporting signals are recon: real detection, but not phone-worthy.
    activity = pipeline.find("CANDIDATE FACTORY ACTIVITY")
    assert activity and activity[0]["category"] == "recon"
    assert state.is_tracked(TOKEN)
    assert state.data["tracked"][TOKEN]["source"] == "chain"
    # topic0 learned for cheaper filtered queries on later launches.
    fstate = state.candidate_factories()[FACTORY.lower()]
    assert fstate["learned_topic0"] == TOPIC_MARKET_CREATED
    assert TOKEN in fstate["confirmed_tokens"]


def test_learned_topic0_used_after_first_token(state, pipeline, errors):
    state.add_candidate_factory(FACTORY, 900)
    state.candidate_factories()[FACTORY.lower()]["learned_topic0"] = TOPIC_MARKET_CREATED
    rpc = FakeRpc()
    watcher = make_watcher(state, pipeline, errors, rpc=rpc)
    watcher.scan_candidate_factories(head=1000)
    scans = [c for c in rpc.get_logs_calls
             if (c["address"] or "").lower() == FACTORY.lower()]
    assert scans and scans[0]["topics"] == [TOPIC_MARKET_CREATED]


def test_clockin_via_chain_path(state, pipeline, errors):
    state.add_candidate_factory(FACTORY, 900)
    rpc = FakeRpc(logs_by_address={FACTORY.lower(): [
        factory_log("0xaaa2", [TOPIC_MARKET_CREATED, pad_address(TOKEN)]),
    ]})
    bs = FakeBlockscout(tokens={TOKEN: {"name": "ClockIn", "symbol": "CLOCKIN",
                                        "total_supply": "1000000000"}})
    watcher = make_watcher(state, pipeline, errors, rpc=rpc, blockscout=bs)
    watcher.run_once()
    clockin = pipeline.find("*** CLOCKIN ***")
    assert clockin and clockin[0]["level"] == "loud"
    assert clockin[0]["text"].splitlines()[0] == TOKEN
    assert "detected via chain" in clockin[0]["text"]


def test_factory_logs_dedupe_survive_restart(tmp_path, pipeline, errors):
    path = str(tmp_path / "state.json")
    state1 = State(path=path)
    state1.add_candidate_factory(FACTORY, 900)
    logs = {FACTORY.lower(): [
        factory_log("0xaaa3", [TOPIC_MARKET_CREATED, pad_address(TOKEN)]),
    ]}
    bs = FakeBlockscout(tokens={TOKEN: {"name": "KIDDIES", "symbol": "KIDDIES"}})
    watcher1 = make_watcher(state1, pipeline, errors,
                            rpc=FakeRpc(logs_by_address=logs), blockscout=bs)
    watcher1.run_once()
    count = len(pipeline.sent)

    state2 = State(path=path)  # restart
    assert state2.factory_log_seen(FACTORY, "0xaaa3:0x0")
    # Same logs replayed (e.g. re-scan overlap) must not re-alert.
    state2.candidate_factories()[FACTORY.lower()]["last_scanned"] = 899
    watcher2 = make_watcher(state2, pipeline, errors,
                            rpc=FakeRpc(logs_by_address=logs), blockscout=bs)
    watcher2.run_once()
    assert len(pipeline.sent) == count, "no duplicate alerts after restart"


def test_topic_to_address():
    assert topic_to_address(pad_address(TOKEN)) == TOKEN
    assert topic_to_address("0x" + "11" * 32) is None      # not zero-padded
    assert topic_to_address("0x" + "0" * 64) is None       # zero address
    assert topic_to_address("0xdeadbeef") is None          # wrong length
    assert topic_to_address(None) is None
