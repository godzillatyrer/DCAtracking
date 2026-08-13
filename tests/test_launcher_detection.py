"""Detecting the LauncherFactory, and surviving ticker collisions.

The mainnet LauncherFactory address is unpublished, so it must be caught at
deploy time. It may be deployed from inside a call, which leaves no
top-level creation tx — the feed the watcher originally relied on.

Separately: six CLOCKIN variants already exist on the StonkBrokers factory
and another squats on pools.fun, so a symbol match proves nothing on its
own. Provenance decides; the name only escalates.
"""
from stonk_watcher import config
from stonk_watcher.chain_watcher import ChainWatcher
from tests.test_chain_watcher import FakeBlockscout, FakeRpc, factory_log

WALLET = config.TEAM_WALLETS[0]  # MASTER EOA, deployed the token factory
LAUNCHER = "0x1acecafe00000000000000000000000000000001"
PLAIN = "0x0badc0de00000000000000000000000000000002"
TOKEN = "0xc10c1c10c1c10c1c10c1c10c1c10c1c10c1c10c1"
LOCKER_V3 = "0xfc96cf67ecc55be4adabc3aecbe6ad6349f11223"


def internal_creation(tx_hash, contract, index=0, block=900):
    return {"transaction_hash": tx_hash, "index": index,
            "created_contract": {"hash": contract}, "block_number": block}


def make_watcher(state, pipeline, errors, bs, rpc=None):
    return ChainWatcher(state, pipeline, errors, rpc=rpc or FakeRpc(),
                        blockscout=bs)


# -- internal-transaction deploys -------------------------------------------

def test_internal_deploy_is_detected(state, pipeline, errors):
    """A factory created from inside a call emits no top-level creation tx."""
    bs = FakeBlockscout(internal_by_wallet={WALLET.lower(): []})
    watcher = make_watcher(state, pipeline, errors, bs)
    watcher.check_internal_creations()          # silent baseline
    assert not pipeline.sent

    bs.internal_by_wallet[WALLET.lower()] = [internal_creation("0xint1", PLAIN)]
    watcher.check_internal_creations()

    assert PLAIN in state.candidate_factories(), (
        "an internally-deployed contract must still be armed")
    alert = pipeline.find("TEAM WALLET DEPLOYED A CONTRACT")
    assert alert and "internal tx" in alert[0]["text"]


def test_launcher_identified_by_its_own_abi(state, pipeline, errors):
    """createLaunch/finalizeLaunch is a positive identification, so it earns
    a launch-grade alert rather than being filtered as routine recon."""
    bs = FakeBlockscout(
        internal_by_wallet={WALLET.lower(): []},
        launchers={LAUNCHER: {"is_launcher": True, "name": "LauncherFactory",
                              "markers": ["createLaunch", "finalizeLaunch"],
                              "known": True}})
    watcher = make_watcher(state, pipeline, errors, bs)
    watcher.check_internal_creations()
    bs.internal_by_wallet[WALLET.lower()] = [internal_creation("0xint2", LAUNCHER)]
    watcher.check_internal_creations()

    found = pipeline.find("*** LAUNCHER FACTORY FOUND ***")
    assert found, "the ABI names it; this is not a guess"
    assert found[0]["category"] == "launch", "must reach the phone"
    assert found[0]["level"] == "loud"
    assert "createLaunch" in found[0]["text"]
    assert LAUNCHER in (found[0]["code_lines"] or [])
    assert state.candidate_factories()[LAUNCHER]["confirmed_launcher"] is True


def test_ordinary_deploy_is_not_called_a_launcher(state, pipeline, errors):
    bs = FakeBlockscout(internal_by_wallet={WALLET.lower(): []})
    watcher = make_watcher(state, pipeline, errors, bs)
    watcher.check_internal_creations()
    bs.internal_by_wallet[WALLET.lower()] = [internal_creation("0xint3", PLAIN)]
    watcher.check_internal_creations()
    assert not pipeline.find("*** LAUNCHER FACTORY FOUND ***")


def test_internal_deploys_dedupe(state, pipeline, errors):
    bs = FakeBlockscout(internal_by_wallet={WALLET.lower(): []})
    watcher = make_watcher(state, pipeline, errors, bs)
    watcher.check_internal_creations()
    bs.internal_by_wallet[WALLET.lower()] = [internal_creation("0xint4", PLAIN)]
    watcher.check_internal_creations()
    count = len(pipeline.sent)
    watcher.check_internal_creations()
    assert len(pipeline.sent) == count


# -- known factories ---------------------------------------------------------

def test_known_stonk_factory_is_armed_at_startup(state, pipeline, errors):
    """CollectionTokenDeployer has already produced 40 tokens including
    CLOCKIN variants — it is a confirmed launch source, not a guess."""
    make_watcher(state, pipeline, errors,
                 FakeBlockscout()).seed_watched_contracts(head=100000)
    for addr in config.KNOWN_STONK_FACTORIES:
        assert addr in state.candidate_factories()
        assert state.candidate_factories()[addr]["source"] == "known_stonk"


# -- Safety Deposit Box LP locks --------------------------------------------

def test_lp_lock_arms_at_head_then_alerts(state, pipeline, errors):
    lock_log = factory_log("0xlock1", [config.TOPIC_POSITION_LOCKED_V3,
                                       "0x" + "00" * 32,
                                       "0x" + "00" * 31 + "07",
                                       "0x" + "0" * 24 + "ab" * 20])
    rpc = FakeRpc(logs_by_address={LOCKER_V3: [lock_log]})
    watcher = make_watcher(state, pipeline, errors, FakeBlockscout(), rpc=rpc)

    watcher.scan_lp_locks(head=1000)   # arm at head; history is not news
    assert not pipeline.find("LP LOCKED")
    assert state.data["lp_locks"][LOCKER_V3] == 1000

    watcher.scan_lp_locks(head=1100)
    alert = pipeline.find("LP LOCKED")
    assert alert and alert[0]["category"] == "launch"
    assert alert[0]["level"] == "loud"


def test_lp_lock_dedupes(state, pipeline, errors):
    lock_log = factory_log("0xlock2", [config.TOPIC_POSITION_LOCKED_V4,
                                       "0x" + "00" * 32])
    rpc = FakeRpc(logs_by_address={
        "0x5a28ce098750f73bc9ec142d4bce464e1a0bbda6": [lock_log]})
    watcher = make_watcher(state, pipeline, errors, FakeBlockscout(), rpc=rpc)
    watcher.scan_lp_locks(head=1000)
    watcher.scan_lp_locks(head=1100)
    count = len(pipeline.find("LP LOCKED"))
    state.data["lp_locks"]["0x5a28ce098750f73bc9ec142d4bce464e1a0bbda6"] = 1000
    watcher.scan_lp_locks(head=1100)
    assert len(pipeline.find("LP LOCKED")) == count


def test_lp_lock_cursor_holds_on_rpc_failure(state, pipeline, errors):
    from stonk_watcher.rpc import RpcError

    class FailingRpc(FakeRpc):
        def get_logs_chunked(self, *a, **k):
            raise RpcError("node down")

    watcher = make_watcher(state, pipeline, errors, FakeBlockscout(),
                           rpc=FailingRpc())
    watcher.scan_lp_locks(head=1000)          # arms
    watcher.scan_lp_locks(head=1100)          # fails
    assert state.data["lp_locks"][LOCKER_V3] == 1000, "window must be retried"


# -- separation and safety ---------------------------------------------------

def test_pools_fun_is_excluded():
    """A separate ecosystem: 196 tokens in 85 minutes. Never a StonkBrokers
    launch, and the source of the earlier alert flood."""
    assert config.PARTY_FACTORY in config.BORING_ADDRESSES
    assert config.PARTY_SWAP_ROUTER in config.BORING_ADDRESSES


def test_testnet_contracts_are_excluded():
    """Docs explicitly warn these must not be used on mainnet."""
    assert "0x631f9371fd6b2c85f8f61d19a90547ee67fa61a2" in config.BORING_ADDRESSES


def test_rpc_uses_curl_user_agent():
    """The RPC 403s the default Python UA; a 403 would blind the chain path
    silently."""
    from stonk_watcher.rpc import RpcClient
    assert RpcClient().session.headers["User-Agent"] == "curl/8.5.0"


def test_log_window_within_rpc_limit():
    assert config.LOG_CHUNK_SIZE <= config.MAX_LOG_WINDOW


# -- ticker collisions -------------------------------------------------------

def test_name_match_alone_is_never_confirmed(monkeypatch):
    """Six CLOCKIN variants exist on the StonkBrokers factory alone. Without
    an expected supply, a name match can only ever be a CANDIDATE."""
    from stonk_watcher.alerts import format_clockin_alert, verdict_for_target
    monkeypatch.setattr(config, "EXPECTED_SUPPLY", "")
    verdict, note = verdict_for_target("1000000")
    assert verdict == "unknown"
    text = format_clockin_alert(TOKEN, "api", "CLOCKIN", "CLOCKIN", "1000000")
    assert "CANDIDATE" in text and "CONFIRMED" not in text


def test_matching_supply_confirms(monkeypatch):
    from stonk_watcher.alerts import format_clockin_alert, verdict_for_target
    monkeypatch.setattr(config, "EXPECTED_SUPPLY", "1000000000")
    assert verdict_for_target("1000000000")[0] == "confirmed"
    text = format_clockin_alert(TOKEN, "api", "CLOCKIN", "CLOCKIN", "1000000000")
    assert "CONFIRMED" in text


def test_wrong_supply_is_never_upgraded(monkeypatch):
    """The TICKERYARD case: a test deploy with 10,000,000,000 supply against
    a publicised spec of 1,999,999,980."""
    from stonk_watcher.alerts import format_clockin_alert, verdict_for_target
    monkeypatch.setattr(config, "EXPECTED_SUPPLY", "1999999980")
    verdict, note = verdict_for_target("10000000000")
    assert verdict == "mismatch"
    assert "SPEC MISMATCH" in note and "10000000000" in note
    text = format_clockin_alert(TOKEN, "api", "YARD", "YARD", "10000000000")
    assert "CANDIDATE" in text and "SPEC MISMATCH" in text
    assert "CONFIRMED" not in text


def test_known_clockin_supply_variants_all_stay_candidates(monkeypatch):
    """The six real variants: 1e6 / 31,536,000 / 568,554,987 / 20,444,424 /
    1e9 / 1e6. With the true supply set, only the right one confirms."""
    from stonk_watcher.alerts import verdict_for_target
    monkeypatch.setattr(config, "EXPECTED_SUPPLY", "568554987")
    variants = ["1000000", "31536000", "568554987", "20444424", "1000000000"]
    verdicts = [verdict_for_target(v)[0] for v in variants]
    assert verdicts.count("confirmed") == 1
    assert verdicts[2] == "confirmed"


def test_test_deploys_are_recognised():
    from stonk_watcher.alerts import is_test_token
    assert is_test_token("TSTDONOTBUY", None)
    assert is_test_token("scram test", None)
    assert is_test_token(None, "DONOTBUY")
    assert not is_test_token("CLOCKIN", "CLOCKIN")


def test_yard_supply_is_known_and_grades_the_test_deploy():
    """YARD's real supply is 1,999,999,980. The TICKERYARD test deploy at
    0x996cbbA6… carries 10,000,000,000 and must never read as confirmed."""
    from stonk_watcher.alerts import verdict_for_target
    assert config.expected_supply_for("YARD") == "1999999980"
    assert verdict_for_target("1999999980", "YARD")[0] == "confirmed"
    verdict, note = verdict_for_target("10000000000", "YARD")
    assert verdict == "mismatch"
    assert "1999999980" in note


def test_symbol_lookup_is_case_and_prefix_insensitive():
    assert config.expected_supply_for("$yard") == "1999999980"
    assert config.expected_supply_for(" YARD ") == "1999999980"


def test_unknown_symbol_stays_candidate():
    """CLOCKIN's real supply is not known, so it must not be confirmed."""
    from stonk_watcher.alerts import verdict_for_target
    assert not config.expected_supply_for("CLOCKIN")
    assert verdict_for_target("1000000", "CLOCKIN")[0] == "unknown"


def test_env_can_add_symbols(monkeypatch):
    import importlib
    monkeypatch.setenv("EXPECTED_SUPPLIES", "CLOCKIN:123456,$FOO:99")
    cfg = importlib.reload(config)
    try:
        assert cfg.expected_supply_for("CLOCKIN") == "123456"
        assert cfg.expected_supply_for("FOO") == "99"
        assert cfg.expected_supply_for("YARD") == "1999999980", "builtin kept"
    finally:
        monkeypatch.delenv("EXPECTED_SUPPLIES", raising=False)
        importlib.reload(config)


# -- LP lock alerts must carry the CA, not just a link -----------------------

def _lock_setup(state, pipeline, errors, transfers):
    lock_log = factory_log("0xlockca", [config.TOPIC_POSITION_LOCKED_V3,
                                        "0x" + "00" * 32,
                                        "0x" + "00" * 31 + "07",
                                        "0x" + "0" * 24 + "ab" * 20])
    rpc = FakeRpc(logs_by_address={LOCKER_V3: [lock_log]})
    bs = FakeBlockscout(tx_transfers={"0xlockca": transfers})
    watcher = make_watcher(state, pipeline, errors, bs, rpc=rpc)
    watcher.scan_lp_locks(head=1000)
    watcher.scan_lp_locks(head=1100)
    return watcher


def _transfer(addr, name, symbol):
    return {"token": {"address": addr, "name": name, "symbol": symbol}}


def test_lp_lock_alert_includes_the_contract_address(state, pipeline, errors):
    """The PositionLocked event carries only position IDs and an owner, so
    the token is resolved from the transaction's transfers — otherwise the
    alert is just a link you have to go dig through."""
    _lock_setup(state, pipeline, errors,
                [_transfer(TOKEN, "ClockIn", "CLOCKIN")])
    alert = pipeline.find("LP LOCKED")
    assert alert
    text = alert[0]["text"]
    assert text.splitlines()[0] == TOKEN, "bare CA first, for copy-paste"
    assert "ClockIn (CLOCKIN)" in text
    assert TOKEN in (alert[0]["code_lines"] or []), "tap-to-copy on Telegram"


def test_quote_assets_are_not_reported_as_the_token(state, pipeline, errors):
    """An LP lock moves WETH/USDG too; naming those as the launch would be
    worse than useless."""
    weth = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
    _lock_setup(state, pipeline, errors,
                [_transfer(weth, "Wrapped Ether", "WETH"),
                 _transfer(TOKEN, "ClockIn", "CLOCKIN")])
    text = pipeline.find("LP LOCKED")[0]["text"]
    assert text.splitlines()[0] == TOKEN
    assert "WETH" not in text


def test_clockin_lock_is_escalated(state, pipeline, errors):
    _lock_setup(state, pipeline, errors,
                [_transfer(TOKEN, "ClockIn", "CLOCKIN")])
    assert pipeline.find("*** CLOCKIN *** LP LOCKED")


def test_lock_still_alerts_when_token_cannot_be_resolved(state, pipeline, errors):
    """Blockscout may be down or the tx may move only quote assets. The lock
    itself is still the signal, so it must not be swallowed."""
    _lock_setup(state, pipeline, errors, [])
    alert = pipeline.find("LP LOCKED")
    assert alert and "could not be resolved" in alert[0]["text"]
    assert alert[0]["category"] == "launch"


# -- factory vs caller -------------------------------------------------------

def test_factory_is_the_called_contract_not_the_caller(monkeypatch, tmp_path):
    """When a user calls launchToken(), Blockscout can record THEIR wallet as
    the token's creator while the factory is the contract the tx was sent to.
    Taking creator at face value would call a real launchpad token
    hand-deployed."""
    import watcher as w
    from stonk_watcher.state import State

    sniper = "0x5111100000000000000000000000000000000001"   # an EOA caller
    factory = "0xfac70fac70fac70fac70fac70fac70fac70fac70"  # the real factory

    class BS:
        def classify_address(self, addr):
            return {"exists": True, "token": {"name": "stonks", "symbol": "STONKS",
                                              "total_supply": "1000"},
                    "is_contract": addr.lower() != sniper}

        def creation_info(self, addr):
            return {"creator": sniper, "block": 1,
                    "called_contract": factory, "creation_tx": "0xabc"}

        def identify_launcher(self, addr):
            return {"is_launcher": addr.lower() == factory,
                    "name": "LauncherFactory" if addr.lower() == factory else None,
                    "markers": ["launchToken"] if addr.lower() == factory else [],
                    "known": True}

    state = State(path=str(tmp_path / "s.json"))
    res = w._describe_ca(BS(), state, TOKEN)
    assert res["creator"] == factory, "the called contract is the factory"
    assert res["deployer"] == sniper, "the caller is reported separately"
    assert res["factory_is_contract"] is True
    assert any("launchToken" in r for r in res["reasons"])
