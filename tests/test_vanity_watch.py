"""Vanity-suffix mint watch.

Launcher tokens are CREATE2-mined to end in `666666`. A random address ends
that way roughly once in 16.7 million, so the suffix is a sharp filter — and
that is exactly what the reverted chain-wide mint watch lacked. The trigger
is the mint itself, the first event a token emits, so this fires the instant
the token exists.
"""
from stonk_watcher import config
from stonk_watcher.vanity_watcher import VanityWatcher
from tests.test_chain_watcher import FakeBlockscout, FakeRpc, pad_address

# The three real tokens from the launch window, all vanity-mined.
STONKS = "0xf65238fb6588baa402f98821f38a11a909666666"
STONKCAT = "0x24cacf27a2ac05eaab50ad312ab02f8ad1666666"
BROKE = "0x36c8baf026e2b1597f4a44bc2b22579e56666666"
ENTRYPOINT = "0x80a77001456bc986083678f9a112b1ec2aa07281"
DEPLOYER = "0x00f8c29b28cb00a20f0ca071efaed0d3fe15dd97"
RANDOM = "0xbeef00000000000000000000000000000000abcd"
HOLDER = "0x1111111111111111111111111111111111111111"


def mint_log(token, tx="0xmint"):
    """Transfer(from=0x0, to=holder) — a token's first event."""
    return {"address": token, "transactionHash": tx, "logIndex": "0x0",
            "topics": [config.TRANSFER_TOPIC0, config.ZERO_TOPIC,
                       pad_address(HOLDER)]}


class VanityRpc(FakeRpc):
    def __init__(self, logs, head=1000):
        super().__init__(head=head)
        self.chain_logs = logs

    def get_logs_chunked(self, address, from_block, to_block, topics=None,
                         chunk=None):
        self.get_logs_calls.append(
            {"address": address, "from": from_block, "topics": topics})
        return self.chain_logs


def make_watcher(state, pipeline, errors, logs, bs=None, head=1000):
    return VanityWatcher(state, pipeline, errors, rpc=VanityRpc(logs, head),
                         blockscout=bs or FakeBlockscout())


def test_arms_at_head_without_replaying_history(state, pipeline, errors):
    """The three tokens already on chain must not all fire on first boot."""
    w = make_watcher(state, pipeline, errors, [mint_log(STONKS)])
    w.run_once()
    assert not pipeline.sent
    assert state.data["vanity_last_scanned"] == 1000


def test_vanity_mint_alerts_immediately(state, pipeline, errors):
    state.data["vanity_last_scanned"] = 999
    bs = FakeBlockscout(
        tokens={STONKS: {"name": "stonks", "symbol": "STONKS",
                         "total_supply": config.LAUNCHER_DEFAULT_SUPPLY}},
        creators={STONKS: {"creator": DEPLOYER, "block": 1000,
                           "called_contract": ENTRYPOINT}})
    make_watcher(state, pipeline, errors, [mint_log(STONKS)], bs).run_once()

    alert = pipeline.find("VANITY TOKEN MINTED")
    assert alert and alert[0]["level"] == "loud"
    assert alert[0]["category"] == "launch"
    assert alert[0]["text"].splitlines()[0] == STONKS, "bare CA first"
    assert STONKS in (alert[0]["code_lines"] or [])
    assert "FROM THE STONK LAUNCHPAD" in alert[0]["text"]
    assert state.is_tracked(STONKS), "armed for the LP watch too"


def test_non_vanity_mints_are_ignored(state, pipeline, errors):
    """This is the whole reason the filter makes a chain-wide scan viable:
    ordinary memecoins minting constantly never reach the phone."""
    state.data["vanity_last_scanned"] = 999
    bs = FakeBlockscout(tokens={RANDOM: {"name": "Verto", "symbol": "VERTO"}})
    w = make_watcher(state, pipeline, errors, [mint_log(RANDOM)], bs)
    w.run_once()
    assert not pipeline.sent
    assert RANDOM not in state.data["vanity_seen"]


def test_scan_is_chain_wide_and_topic_filtered(state, pipeline, errors):
    state.data["vanity_last_scanned"] = 999
    w = make_watcher(state, pipeline, errors, [])
    w.run_once()
    call = w.rpc.get_logs_calls[0]
    assert call["address"] is None, "no address filter — the suffix is the filter"
    assert call["topics"] == [config.TRANSFER_TOPIC0, config.ZERO_TOPIC]


def test_each_token_alerts_once(state, pipeline, errors):
    state.data["vanity_last_scanned"] = 999
    bs = FakeBlockscout(tokens={STONKS: {"name": "stonks", "symbol": "STONKS"}})
    w = make_watcher(state, pipeline, errors,
                     [mint_log(STONKS, "0xa"), mint_log(STONKS, "0xb")], bs)
    w.run_once()
    assert len(pipeline.find("VANITY TOKEN MINTED")) == 1
    state.data["vanity_last_scanned"] = 999
    w.run_once()
    assert len(pipeline.find("VANITY TOKEN MINTED")) == 1


def test_all_three_launch_tokens_would_be_caught(state, pipeline, errors):
    state.data["vanity_last_scanned"] = 999
    tokens = {t: {"name": n, "symbol": n.upper()} for t, n in
              ((STONKS, "stonks"), (STONKCAT, "stonkcat"), (BROKE, "broke"))}
    bs = FakeBlockscout(tokens=tokens, creators={
        t: {"creator": DEPLOYER, "block": 1000, "called_contract": ENTRYPOINT}
        for t in tokens})
    make_watcher(state, pipeline, errors,
                 [mint_log(STONKS, "0xa"), mint_log(STONKCAT, "0xb"),
                  mint_log(BROKE, "0xc")], bs).run_once()
    assert len(pipeline.find("VANITY TOKEN MINTED")) == 3


def test_clockin_escalates(state, pipeline, errors):
    state.data["vanity_last_scanned"] = 999
    bs = FakeBlockscout(
        tokens={STONKS: {"name": "ClockIn", "symbol": "CLOCKIN"}},
        creators={STONKS: {"creator": DEPLOYER, "block": 1000,
                           "called_contract": ENTRYPOINT}})
    make_watcher(state, pipeline, errors, [mint_log(STONKS)], bs).run_once()
    assert pipeline.find("*** CLOCKIN ***")


def test_alerts_even_when_metadata_is_unavailable(state, pipeline, errors):
    """A brand-new contract is often not indexed yet. The CA is the point —
    waiting for a name would trade away the speed this exists for."""
    import requests as _requests

    class DownBlockscout(FakeBlockscout):
        def classify_address(self, address):
            raise _requests.RequestException("not indexed")

        def creation_info(self, address):
            raise _requests.RequestException("not indexed")

    state.data["vanity_last_scanned"] = 999
    make_watcher(state, pipeline, errors, [mint_log(STONKS)],
                 DownBlockscout()).run_once()
    alert = pipeline.find("VANITY TOKEN MINTED")
    assert alert, "the alert must fire regardless"
    assert alert[0]["text"].splitlines()[0] == STONKS
    assert "not verified yet" in alert[0]["text"]


def test_non_launchpad_vanity_is_labelled_not_hidden(state, pipeline, errors):
    """Anyone can mine the suffix. An imitator still alerts, but is called
    out rather than passed off as a launchpad token."""
    state.data["vanity_last_scanned"] = 999
    imposter = "0xdead000000000000000000000000000000666666"
    bs = FakeBlockscout(
        tokens={imposter: {"name": "Fake", "symbol": "FAKE"}},
        creators={imposter: {"creator": RANDOM, "block": 1000,
                             "called_contract": RANDOM}})
    make_watcher(state, pipeline, errors, [mint_log(imposter)], bs).run_once()
    text = pipeline.find("VANITY TOKEN MINTED")[0]["text"]
    assert "NOT from the known launchpad" in text
    assert RANDOM in text
    assert not state.is_tracked(imposter), "not armed as a launchpad token"


def test_cursor_holds_on_rpc_failure(state, pipeline, errors):
    from stonk_watcher.rpc import RpcError

    class FailingRpc(VanityRpc):
        def get_logs_chunked(self, *a, **k):
            raise RpcError("node down")

    state.data["vanity_last_scanned"] = 999
    w = VanityWatcher(state, pipeline, errors, rpc=FailingRpc([]),
                      blockscout=FakeBlockscout())
    w.run_once()
    assert state.data["vanity_last_scanned"] == 999, "window is retried"


def test_long_stall_does_not_request_an_enormous_range(state, pipeline, errors):
    state.data["vanity_last_scanned"] = 1
    w = make_watcher(state, pipeline, errors, [], head=500_000)
    w.run_once()
    call = w.rpc.get_logs_calls[0]
    assert call["from"] >= 500_000 - config.MAX_LOG_WINDOW


def test_poll_interval_is_fast_and_configurable():
    w = VanityWatcher.__new__(VanityWatcher)
    assert w.interval() == float(config.VANITY_POLL_INTERVAL)
    assert config.VANITY_POLL_INTERVAL <= 5, "must be seconds, not minutes"


def test_can_be_disabled(state, pipeline, errors, monkeypatch):
    monkeypatch.setattr(config, "VANITY_WATCH", False)
    state.data["vanity_last_scanned"] = 999
    make_watcher(state, pipeline, errors, [mint_log(STONKS)]).run_once()
    assert not pipeline.sent
