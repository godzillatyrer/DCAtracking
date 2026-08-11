"""LP seeding detection for launchpad tokens, on up. or any other venue.

up.'s factory address is unpublished, so this watches from the token's side:
Transfer events emitted by tokens already confirmed as launchpad tokens. The
first transfer into a contract is the pool being funded.

That inversion is what makes it both venue-agnostic and safe — the getLogs
address filter IS the confirmed-token list, so an unrelated memecoin getting
liquidity can never reach the phone.
"""
from stonk_watcher import config
from stonk_watcher.chain_watcher import ChainWatcher
from tests.test_chain_watcher import FakeBlockscout, FakeRpc, pad_address

TOKEN = "0xc10c1c10c1c10c1c10c1c10c1c10c1c10c1c10c1"
STRANGER = "0xbaad0000000000000000000000000000000000ff"
POOL = "0x9001900190019001900190019001900190019001"
UP_FACTORY = "0x11223344556677889900aabbccddeeff00112233"
HOLDER = "0xeeee000000000000000000000000000000000001"


def transfer_log(token, to, tx="0xlp1", index=0):
    return {"address": token, "transactionHash": tx, "logIndex": hex(index),
            "topics": [config.TRANSFER_TOPIC0, pad_address(HOLDER),
                       pad_address(to)]}


class PoolBlockscout(FakeBlockscout):
    def __init__(self, contracts=(), creators=None):
        super().__init__(creators=creators)
        self.contracts = {c.lower() for c in contracts}

    def classify_address(self, address):
        return {"exists": True, "is_contract": address.lower() in self.contracts,
                "token": None}


def make_watcher(state, pipeline, errors, logs, bs):
    class LogRpc(FakeRpc):
        def get_logs_chunked(self, address, from_block, to_block,
                             topics=None, chunk=None):
            self.get_logs_calls.append({"address": address, "topics": topics})
            return logs

    return ChainWatcher(state, pipeline, errors, rpc=LogRpc(), blockscout=bs)


def test_lp_seeding_alerts_for_a_launchpad_token(state, pipeline, errors):
    state.add_tracked(TOKEN, source="api", name="ClockIn", symbol="CLOCKIN")
    bs = PoolBlockscout(contracts=[POOL],
                        creators={POOL: {"creator": UP_FACTORY, "block": 10}})
    watcher = make_watcher(state, pipeline, errors, [transfer_log(TOKEN, POOL)], bs)

    watcher.scan_tracked_liquidity(head=1000)   # arms at head
    assert not pipeline.find("LP SEEDED")

    watcher.scan_tracked_liquidity(head=1100)
    alert = pipeline.find("LP SEEDED")
    assert alert and alert[0]["level"] == "loud"
    assert alert[0]["category"] == "launch"
    assert TOKEN in alert[0]["text"] and POOL in alert[0]["text"]
    assert TOKEN in (alert[0]["code_lines"] or [])
    assert POOL in state.data["token_pools"][TOKEN]


def test_unknown_venue_reports_the_deployer_so_up_can_be_learned(
        state, pipeline, errors):
    """up.'s factory is unpublished; the first graduation reveals it."""
    state.add_tracked(TOKEN, source="api", name="ClockIn", symbol="CLOCKIN")
    bs = PoolBlockscout(contracts=[POOL],
                        creators={POOL: {"creator": UP_FACTORY, "block": 10}})
    watcher = make_watcher(state, pipeline, errors, [transfer_log(TOKEN, POOL)], bs)
    watcher.scan_tracked_liquidity(head=1000)
    watcher.scan_tracked_liquidity(head=1100)

    text = pipeline.find("LP SEEDED")[0]["text"]
    assert UP_FACTORY in text
    assert "UP_DEX_CONTRACTS" in text, "tells you how to label it next time"


def test_configured_up_address_is_named(state, pipeline, errors, monkeypatch):
    monkeypatch.setitem(config.KNOWN_DEX_LABELS, UP_FACTORY, "up. (up33.xyz)")
    state.add_tracked(TOKEN, source="api", name="ClockIn", symbol="CLOCKIN")
    bs = PoolBlockscout(contracts=[POOL],
                        creators={POOL: {"creator": UP_FACTORY, "block": 10}})
    watcher = make_watcher(state, pipeline, errors, [transfer_log(TOKEN, POOL)], bs)
    watcher.scan_tracked_liquidity(head=1000)
    watcher.scan_tracked_liquidity(head=1100)

    text = pipeline.find("LP SEEDED")[0]["text"]
    assert "up. (up33.xyz)" in text
    assert config.UP_DEX_URL in text, "link straight to the trade page"


def test_only_tracked_tokens_are_queried(state, pipeline, errors):
    """The address filter IS the provenance gate — this is what stops the
    unrelated-memecoin flood from coming back through the LP path."""
    state.add_tracked(TOKEN, source="api", name="ClockIn", symbol="CLOCKIN")
    bs = PoolBlockscout(contracts=[POOL])
    watcher = make_watcher(state, pipeline, errors, [], bs)
    watcher.scan_tracked_liquidity(head=1000)
    watcher.scan_tracked_liquidity(head=1100)
    call = watcher.rpc.get_logs_calls[0]
    assert call["address"] == [TOKEN], "never a chain-wide query"
    assert call["topics"] == [config.TRANSFER_TOPIC0]


def test_untracked_token_never_alerts(state, pipeline, errors):
    state.add_tracked(TOKEN, source="api", name="ClockIn", symbol="CLOCKIN")
    bs = PoolBlockscout(contracts=[POOL])
    # A stray log for a token we do not track must be ignored outright.
    watcher = make_watcher(state, pipeline, errors,
                           [transfer_log(STRANGER, POOL)], bs)
    watcher.scan_tracked_liquidity(head=1000)
    watcher.scan_tracked_liquidity(head=1100)
    assert not pipeline.find("LP SEEDED")


def test_transfers_to_wallets_are_not_pools(state, pipeline, errors):
    state.add_tracked(TOKEN, source="api", name="ClockIn", symbol="CLOCKIN")
    bs = PoolBlockscout(contracts=[])          # recipient is an EOA
    watcher = make_watcher(state, pipeline, errors,
                           [transfer_log(TOKEN, HOLDER)], bs)
    watcher.scan_tracked_liquidity(head=1000)
    watcher.scan_tracked_liquidity(head=1100)
    assert not pipeline.find("LP SEEDED"), "an ordinary holder is not a pool"


def test_plumbing_recipients_are_skipped(state, pipeline, errors):
    """Lockers and position managers receive tokens but are not pools."""
    state.add_tracked(TOKEN, source="api", name="ClockIn", symbol="CLOCKIN")
    manager = "0x73991a25c818bf1f1128deaab1492d45638de0d3"
    bs = PoolBlockscout(contracts=[manager])
    watcher = make_watcher(state, pipeline, errors,
                           [transfer_log(TOKEN, manager)], bs)
    watcher.scan_tracked_liquidity(head=1000)
    watcher.scan_tracked_liquidity(head=1100)
    assert not pipeline.find("LP SEEDED")


def test_pool_alerts_once(state, pipeline, errors):
    state.add_tracked(TOKEN, source="api", name="ClockIn", symbol="CLOCKIN")
    bs = PoolBlockscout(contracts=[POOL],
                        creators={POOL: {"creator": UP_FACTORY, "block": 10}})
    watcher = make_watcher(state, pipeline, errors, [
        transfer_log(TOKEN, POOL, tx="0xlp1"),
        transfer_log(TOKEN, POOL, tx="0xlp2", index=1)], bs)
    watcher.scan_tracked_liquidity(head=1000)
    watcher.scan_tracked_liquidity(head=1100)
    assert len(pipeline.find("LP SEEDED")) == 1, "repeat transfers are trading"


def test_cursor_holds_on_rpc_failure(state, pipeline, errors):
    from stonk_watcher.rpc import RpcError

    state.add_tracked(TOKEN, source="api", name="ClockIn", symbol="CLOCKIN")

    class FailingRpc(FakeRpc):
        def get_logs_chunked(self, *a, **k):
            raise RpcError("node down")

    watcher = ChainWatcher(state, pipeline, errors, rpc=FailingRpc(),
                           blockscout=PoolBlockscout())
    watcher.scan_tracked_liquidity(head=1000)
    watcher.scan_tracked_liquidity(head=1100)
    assert state.data["liquidity_last_scanned"] == 1000, "window is retried"


def test_blockscout_failure_leaves_pool_unrecorded(state, pipeline, errors):
    """A transient failure must not mark the pool seen and lose the alert."""
    import requests as _requests

    state.add_tracked(TOKEN, source="api", name="ClockIn", symbol="CLOCKIN")

    class DownBlockscout(PoolBlockscout):
        down = True

        def classify_address(self, address):
            if self.down:
                raise _requests.RequestException("500")
            return super().classify_address(address)

    bs = DownBlockscout(contracts=[POOL],
                        creators={POOL: {"creator": UP_FACTORY, "block": 10}})
    watcher = make_watcher(state, pipeline, errors, [transfer_log(TOKEN, POOL)], bs)
    watcher.scan_tracked_liquidity(head=1000)
    watcher.scan_tracked_liquidity(head=1100)
    assert POOL not in state.data["token_pools"].get(TOKEN, [])

    bs.down = False
    state.data["liquidity_last_scanned"] = 1100
    watcher.scan_tracked_liquidity(head=1200)
    assert pipeline.find("LP SEEDED"), "recovers on the next cycle"


def test_liquidity_watch_can_be_disabled(state, pipeline, errors, monkeypatch):
    monkeypatch.setattr(config, "LIQUIDITY_WATCH", False)
    state.add_tracked(TOKEN, source="api", name="ClockIn", symbol="CLOCKIN")
    bs = PoolBlockscout(contracts=[POOL])
    watcher = make_watcher(state, pipeline, errors, [transfer_log(TOKEN, POOL)], bs)
    watcher.scan_tracked_liquidity(head=1000)
    watcher.scan_tracked_liquidity(head=1100)
    assert not pipeline.sent
