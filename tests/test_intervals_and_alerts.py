"""Acceptance test 4: poll intervals tighten on schedule around T0.
Plus CLOCKIN alert formatting and error-reporter rate limiting."""
from datetime import datetime, timedelta, timezone

from stonk_watcher import config
from stonk_watcher.alerts import ErrorReporter, format_clockin_alert, is_target_token

T0 = datetime(2026, 8, 12, 0, 0, 0, tzinfo=timezone.utc)


def at(delta_hours):
    return T0 + timedelta(hours=delta_hours)


def test_api_poll_schedule():
    assert config.api_poll_interval(at(-30), T0) == 10   # >24h out
    assert config.api_poll_interval(at(-12), T0) == 5    # 24h -> 1h
    assert config.api_poll_interval(at(-2), T0) == 5
    assert config.api_poll_interval(at(-0.5), T0) == 2   # war room opens
    assert config.api_poll_interval(at(0), T0) == 2
    assert config.api_poll_interval(at(1.9), T0) == 2    # war room closes at +2h
    assert config.api_poll_interval(at(3), T0) == 10     # stay running after


def test_chain_poll_schedule():
    assert config.chain_poll_interval(at(-30), T0) == 20
    assert config.chain_poll_interval(at(-2), T0) == 20
    assert config.chain_poll_interval(at(-0.5), T0) == 5
    assert config.chain_poll_interval(at(1.5), T0) == 5
    assert config.chain_poll_interval(at(3), T0) == 20


def test_t0_two_minutes_out_is_war_room():
    """Acceptance test 4 as specified: set T0 to now+2min -> tight intervals."""
    now = datetime.now(timezone.utc)
    t0 = now + timedelta(minutes=2)
    assert config.api_poll_interval(now, t0) == 2
    assert config.chain_poll_interval(now, t0) == 5


def test_t0_env_override(monkeypatch):
    monkeypatch.setenv("T0_UTC", "2026-08-12T00:00:00Z")
    assert config.get_t0() == T0
    monkeypatch.setenv("T0_UTC", "not-a-date")
    assert config.get_t0() == config.DEFAULT_T0


CA = "0x4444444444444444444444444444444444444444"


def test_clockin_alert_format():
    text = format_clockin_alert(CA, "api", "CLOCKIN", "CLOCKIN", "1000000000",
                                raw={"a": 1})
    lines = text.splitlines()
    assert lines[0] == CA, "first line must be the bare CA on its own line"
    assert lines[1] == "*** CLOCKIN *** detected via api"
    assert lines[2] == CA
    assert f"token: {config.BLOCKSCOUT_BASE}/token/{CA}" in text
    assert "site:  https://www.stonkbrokers.cash/launcher" in text
    assert 'raw:   {"a":1}' in text
    assert "tx:" not in text  # api-sourced alert has no tx link


def test_clockin_alert_chain_variant():
    tx = "https://robinhoodchain.blockscout.com/tx/0xabc"
    text = format_clockin_alert(CA, "chain", "ClockIn", "CLK", "5", tx_link=tx)
    assert "detected via chain" in text
    assert f"tx:    {tx}" in text
    assert "raw:" not in text


def test_is_target_token_matching():
    assert is_target_token("CLOCKIN", None)
    assert is_target_token(None, "clockin")
    assert is_target_token("ClockIn", "XYZ")
    assert is_target_token("$CLOCKIN", None)      # ticker prefix stripped
    assert is_target_token(" clockin ", None)     # whitespace tolerated
    assert not is_target_token("CLOCKIN2", "CLK")
    assert not is_target_token("overclocking", None)
    assert not is_target_token(None, None)


def test_error_reporter_rate_limit(pipeline):
    reporter = ErrorReporter(pipeline, cooldown=3600)
    reporter.report("api_poller", "boom", key="k1")
    reporter.report("api_poller", "boom", key="k1")
    reporter.report("api_poller", "boom", key="k1")
    assert len(pipeline.find("WATCHER ERROR")) == 1, "identical errors coalesce"
    reporter.report("chain_watcher", "different", key="k2")
    assert len(pipeline.find("WATCHER ERROR")) == 2, "distinct errors still alert"


def test_error_reporter_recovery_message(pipeline):
    reporter = ErrorReporter(pipeline, cooldown=0)
    reporter.recovered("api_poller", "back to normal")
    assert pipeline.find("WATCHER RECOVERED")
