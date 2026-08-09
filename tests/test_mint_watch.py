"""Chain-wide mint watch.

Every other on-chain path only sees a token if it came out of a factory we
already knew to watch. A mint (Transfer from the zero address) is
unavoidable — a token cannot be distributed without one — so this is the
path that catches CLOCKIN even if the launcher factory is deployed by a
wallet we never see, or already existed before we started.
"""
from stonk_watcher import config
from stonk_watcher.chain_watcher import ChainWatcher
from stonk_watcher.state import State
from tests.test_chain_watcher import FakeBlockscout, FakeRpc, pad_address

CLOCKIN_CA = "0xc10c1c10c1c10c1c10c1c10c1c10c1c10c1c10c1"
OTHER_CA = "0xbeef000000000000000000000000000000000001"
NFT_CA = "0xbeef000000000000000000000000000000000002"
HOLDER = "0x1111111111111111111111111111111111111111"


def mint_log(token, tx="0xmint1"):
    """Transfer(from=0x0, to=holder) — the universal new-token signature."""
    return {"address": token, "transactionHash": tx, "logIndex": "0x0",
            "topics": [config.TRANSFER_TOPIC0, config.ZERO_TOPIC,
                       pad_address(HOLDER)]}


class MintRpc(FakeRpc):
    """Chain-wide getLogs: address filter is None, topics select mints."""

    def __init__(self, logs, head=1000):
        super().__init__(head=head)
        self.chain_logs = logs

    def get_logs_chunked(self, address, from_block, to_block, topics=None, chunk=None):
        self.get_logs_calls.append(
            {"address": address, "from": from_block, "to": to_block, "topics": topics})
        if address is None:
            return self.chain_logs
        return []


def make_watcher(state, pipeline, errors, logs, tokens, head=1000):
    return ChainWatcher(state, pipeline, errors,
                        rpc=MintRpc(logs, head=head),
                        blockscout=FakeBlockscout(tokens=tokens))


def test_first_run_arms_at_head_without_scanning_history(state, pipeline, errors):
    watcher = make_watcher(state, pipeline, errors, [mint_log(OTHER_CA)],
                           {OTHER_CA: {"name": "Old", "symbol": "OLD"}})
    watcher.scan_new_mints(head=1000)
    assert state.data["mint_last_scanned"] == 1000
    assert not pipeline.sent, "must not alert on tokens that already existed"


def test_clockin_mint_is_caught_without_any_known_factory(state, pipeline, errors):
    """The whole point: no factory, no team wallet, no API entry — just a
    mint appearing on chain."""
    state.data["mint_last_scanned"] = 999
    watcher = make_watcher(state, pipeline, errors, [mint_log(CLOCKIN_CA)],
                           {CLOCKIN_CA: {"name": "CLOCKIN", "symbol": "CLOCKIN",
                                         "total_supply": "1000000000",
                                         "type": "ERC-20"}})
    watcher.scan_new_mints(head=1000)

    assert not state.candidate_factories(), "no factory was ever known"
    alert = pipeline.find("*** CLOCKIN ***")
    assert alert and alert[0]["level"] == "loud"
    assert alert[0]["category"] == "launch"
    assert alert[0]["text"].splitlines()[0] == CLOCKIN_CA
    assert "first mint" in alert[0]["text"]
    assert state.is_tracked(CLOCKIN_CA)


def test_chain_wide_query_uses_no_address_filter(state, pipeline, errors):
    state.data["mint_last_scanned"] = 999
    watcher = make_watcher(state, pipeline, errors, [], {})
    watcher.scan_new_mints(head=1000)
    call = watcher.rpc.get_logs_calls[0]
    assert call["address"] is None, "must scan the whole chain, not one address"
    assert call["topics"] == [config.TRANSFER_TOPIC0, config.ZERO_TOPIC]


def test_ordinary_new_token_alerts_as_launch(state, pipeline, errors):
    state.data["mint_last_scanned"] = 999
    watcher = make_watcher(state, pipeline, errors, [mint_log(OTHER_CA)],
                           {OTHER_CA: {"name": "Some Coin", "symbol": "SOME",
                                       "type": "ERC-20"}})
    watcher.scan_new_mints(head=1000)
    alert = pipeline.find("NEW TOKEN MINTED ON CHAIN")
    assert alert and alert[0]["category"] == "launch"


def test_nft_mints_are_ignored(state, pipeline, errors):
    state.data["mint_last_scanned"] = 999
    watcher = make_watcher(state, pipeline, errors, [mint_log(NFT_CA)],
                           {NFT_CA: {"name": "Pixel Punks", "symbol": "PUNK",
                                     "type": "ERC-721"}})
    watcher.scan_new_mints(head=1000)
    assert not pipeline.sent, "NFT mints are background noise on this chain"


def test_clockin_nft_still_alerts(state, pipeline, errors):
    """A name match beats the NFT filter — never miss it on a technicality."""
    state.data["mint_last_scanned"] = 999
    watcher = make_watcher(state, pipeline, errors, [mint_log(NFT_CA)],
                           {NFT_CA: {"name": "CLOCKIN", "symbol": "CLOCKIN",
                                     "type": "ERC-721"}})
    watcher.scan_new_mints(head=1000)
    assert pipeline.find("*** CLOCKIN ***")


def test_repeat_mints_do_not_realert(state, pipeline, errors):
    state.data["mint_last_scanned"] = 999
    tokens = {OTHER_CA: {"name": "Some Coin", "symbol": "SOME", "type": "ERC-20"}}
    make_watcher(state, pipeline, errors, [mint_log(OTHER_CA)], tokens
                 ).scan_new_mints(head=1000)
    count = len(pipeline.sent)
    state.data["mint_last_scanned"] = 1000
    make_watcher(state, pipeline, errors,
                 [mint_log(OTHER_CA, tx="0xmint2")], tokens
                 ).scan_new_mints(head=1001)
    assert len(pipeline.sent) == count, "only the first mint of a token alerts"


def test_cursor_holds_on_rpc_failure(state, pipeline, errors):
    """A failed range must be retried, not skipped past."""
    state.data["mint_last_scanned"] = 999

    class FailingRpc(MintRpc):
        def get_logs_chunked(self, *a, **k):
            from stonk_watcher.rpc import RpcError
            raise RpcError("node unavailable")

    watcher = ChainWatcher(state, pipeline, errors, rpc=FailingRpc([]),
                           blockscout=FakeBlockscout())
    watcher.scan_new_mints(head=1000)
    assert state.data["mint_last_scanned"] == 999, "cursor must not advance"


def test_mint_watch_survives_restart(tmp_path, pipeline, errors):
    path = str(tmp_path / "state.json")
    state1 = State(path=path)
    tokens = {OTHER_CA: {"name": "Some Coin", "symbol": "SOME", "type": "ERC-20"}}
    state1.data["mint_last_scanned"] = 999
    make_watcher(state1, pipeline, errors, [mint_log(OTHER_CA)], tokens
                 ).scan_new_mints(head=1000)
    count = len(pipeline.sent)

    state2 = State(path=path)
    assert OTHER_CA in state2.data["mint_seen_tokens"]
    state2.data["mint_last_scanned"] = 999
    make_watcher(state2, pipeline, errors, [mint_log(OTHER_CA)], tokens
                 ).scan_new_mints(head=1000)
    assert len(pipeline.sent) == count


def test_watch_contracts_are_armed(state, pipeline, errors, monkeypatch):
    """A factory address learned by other means is covered immediately."""
    factory = "0xfac70fac70fac70fac70fac70fac70fac70fac70"
    monkeypatch.setattr(config, "WATCH_CONTRACTS", [factory])
    watcher = make_watcher(state, pipeline, errors, [], {})
    watcher.seed_watched_contracts(head=10000)
    assert factory in state.candidate_factories()
    expected = 10000 - config.WATCH_CONTRACTS_LOOKBACK
    assert state.candidate_factories()[factory]["deploy_block"] == expected
