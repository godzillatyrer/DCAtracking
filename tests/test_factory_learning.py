"""Learning the real launcher factory from a confirmed launchpad token.

A chain-wide token filter cannot tell a launchpad token from an unrelated
memecoin, and once the factory *is* known its own logs already cover it. So
the only trustworthy definition of "from the launchpad" is provenance: the
token appeared in the launcher API, or it came out of a contract we have
confirmed is the launcher factory.

Whatever contract deployed an API-listed token *is* that factory, which is
how it gets learned without guessing which wallet deployed it.
"""
from stonk_watcher import config
from stonk_watcher.chain_watcher import ChainWatcher
from tests.test_chain_watcher import FakeBlockscout, FakeRpc

TOKEN = "0xc10c1c10c1c10c1c10c1c10c1c10c1c10c1c10c1"
FACTORY = "0xfac70fac70fac70fac70fac70fac70fac70fac70"
EOA = "0xea0ea0ea0ea0ea0ea0ea0ea0ea0ea0ea0ea0ea0e"


class CreatorBlockscout(FakeBlockscout):
    """Distinguishes contract creators from EOA creators."""

    def __init__(self, creators=None, contracts=()):
        super().__init__(creators=creators)
        self.contracts = {c.lower() for c in contracts}

    def classify_address(self, address):
        is_contract = address.lower() in self.contracts
        return {"exists": True, "is_contract": is_contract, "token": None}


def make_watcher(state, pipeline, errors, blockscout):
    return ChainWatcher(state, pipeline, errors, rpc=FakeRpc(),
                        blockscout=blockscout)


def test_factory_learned_from_api_token(state, pipeline, errors):
    state.add_tracked(TOKEN, source="api", name="CLOCKIN", symbol="CLOCKIN")
    bs = CreatorBlockscout(
        creators={TOKEN: {"creator": FACTORY, "block": 500}},
        contracts=[FACTORY])
    make_watcher(state, pipeline, errors, bs).learn_factories_from_tracked(head=1000)

    assert FACTORY in state.candidate_factories(), (
        "the contract that deployed a launchpad token IS the launcher factory")
    assert state.candidate_factories()[FACTORY]["deploy_block"] == 500
    learned = pipeline.find("LEARNED LAUNCHER FACTORY")
    assert learned and learned[0]["category"] == "health"


def test_non_api_tokens_never_teach_a_factory(state, pipeline, errors):
    """Reproduction of the production incident: tokens tracked by the
    removed mint watch (VERTO, BOT, steakUSDG) taught generic memecoin
    factories. Only API-listed tokens carry launchpad provenance."""
    state.add_tracked(TOKEN, source="mint", name="VERTO", symbol="VERTO")
    other = "0xbeef000000000000000000000000000000000001"
    state.add_tracked(other, source="chain", name="X", symbol="X")
    bs = CreatorBlockscout(
        creators={TOKEN: {"creator": FACTORY, "block": 500},
                  other: {"creator": FACTORY, "block": 501}},
        contracts=[FACTORY])
    make_watcher(state, pipeline, errors, bs).learn_factories_from_tracked(head=1000)

    assert not state.candidate_factories(), (
        "a token of non-API provenance must never arm a factory")
    assert not pipeline.find("LEARNED LAUNCHER FACTORY")


def test_eoa_deployed_token_does_not_arm_a_factory(state, pipeline, errors):
    """A hand-deployed token has no factory behind it — arming the EOA would
    watch a wallet, not a launchpad."""
    state.add_tracked(TOKEN, source="api", name="Manual", symbol="MAN")
    bs = CreatorBlockscout(creators={TOKEN: {"creator": EOA, "block": 500}},
                           contracts=[])
    make_watcher(state, pipeline, errors, bs).learn_factories_from_tracked(head=1000)
    assert not state.candidate_factories()
    assert not pipeline.find("LEARNED LAUNCHER FACTORY")


def test_creator_resolved_only_once(state, pipeline, errors):
    state.add_tracked(TOKEN, source="api", name="CLOCKIN", symbol="CLOCKIN")
    bs = CreatorBlockscout(creators={TOKEN: {"creator": FACTORY, "block": 500}},
                           contracts=[FACTORY])
    watcher = make_watcher(state, pipeline, errors, bs)
    watcher.learn_factories_from_tracked(head=1000)
    count = len(pipeline.sent)
    watcher.learn_factories_from_tracked(head=1001)
    assert len(pipeline.sent) == count, "no repeat lookups or alerts"
    assert state.data["tracked"][TOKEN]["creator_resolved"]


def test_unknown_creation_block_falls_back_to_lookback(state, pipeline, errors):
    state.add_tracked(TOKEN, source="api", name="CLOCKIN", symbol="CLOCKIN")
    bs = CreatorBlockscout(creators={TOKEN: {"creator": FACTORY, "block": None}},
                           contracts=[FACTORY])
    head = config.WATCH_CONTRACTS_LOOKBACK + 9000
    make_watcher(state, pipeline, errors, bs).learn_factories_from_tracked(head=head)
    expected = head - config.WATCH_CONTRACTS_LOOKBACK
    assert state.candidate_factories()[FACTORY]["deploy_block"] == expected


def test_boring_creator_is_ignored(state, pipeline, errors):
    state.add_tracked(TOKEN, source="api", name="X", symbol="X")
    deployer = "0x4e59b44847b379578588920ca78fbf26c0b4956c"  # deterministic deployer
    bs = CreatorBlockscout(creators={TOKEN: {"creator": deployer, "block": 500}},
                           contracts=[deployer])
    make_watcher(state, pipeline, errors, bs).learn_factories_from_tracked(head=1000)
    assert deployer not in state.candidate_factories(), (
        "a universal deployer is used by everyone, not just the launchpad")


def test_no_chain_wide_token_scanning_remains(state, pipeline, errors):
    """Regression guard: nothing may alert on tokens of unknown provenance."""
    import inspect

    from stonk_watcher import chain_watcher

    source = inspect.getsource(chain_watcher)
    assert "NEW TOKEN MINTED ON CHAIN" not in source
    assert "scan_new_mints" not in source
    # A chain-wide getLogs (address=None) must not be reachable here.
    assert "get_logs_chunked(\n                None" not in source
