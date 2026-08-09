"""Acceptance test 1: mocked /api/launcher/tokens returns a token object ->
full alert fires, CA lands in `tracked`, the detail endpoint is called, and
dedupe holds on repeat. Acceptance test 5 (restart survival) for the API path.
"""
import json

from stonk_watcher import config
from stonk_watcher.api_poller import ApiPoller, extract_ca
from stonk_watcher.state import State

CA = "0x1111111111111111111111111111111111111111"
TOKEN_ENTRY = {"address": CA, "name": "CLOCKIN", "symbol": "CLOCKIN",
               "supply": "1000000000"}
BORING_ENTRY = {"address": "0x2222222222222222222222222222222222222222",
                "name": "Some Other Token", "symbol": "SOT"}


def make_poller(state, pipeline, errors, tokens, highlights=None):
    poller = ApiPoller(state, pipeline, errors)
    poller.detail_calls = []

    def fake_fetch(url):
        if url == config.API_TOKENS_URL:
            return {"ok": True, "tokens": tokens, "hasMore": False}
        if url == config.API_HIGHLIGHTS_URL:
            return highlights or {"ok": True, "king": None, "top": None, "hot": None}
        if "/api/launcher/token/" in url:
            poller.detail_calls.append(url)
            return {"ok": True, "token": {"name": "CLOCKIN", "symbol": "CLOCKIN",
                                          "total_supply": "1000000000"}}
        raise AssertionError(f"unexpected url {url}")

    poller._fetch_json = fake_fetch
    return poller


def test_new_token_fires_clockin_alert_and_tracks(state, pipeline, errors):
    poller = make_poller(state, pipeline, errors, [TOKEN_ENTRY])
    poller.run_once()

    clockin = pipeline.find("*** CLOCKIN ***")
    assert clockin, "CLOCKIN alert must fire"
    alert = clockin[0]
    assert alert["level"] == "loud"
    assert alert["category"] == "launch", "must survive the ALERTS_MODE filter"
    # First line = bare CA for copy-paste speed.
    assert alert["text"].splitlines()[0] == CA
    assert CA in (alert["code_lines"] or [])
    # Detail endpoint was called with the CA.
    assert poller.detail_calls == [config.API_TOKEN_DETAIL_URL.format(ca=CA)]
    # CA landed in tracked with source "api".
    assert state.is_tracked(CA)
    assert state.data["tracked"][CA]["source"] == "api"
    # Raw JSON is included.
    assert "raw:" in alert["text"]


def test_non_clockin_token_gets_regular_alert(state, pipeline, errors):
    poller = make_poller(state, pipeline, errors, [BORING_ENTRY])

    def fake_detail(url):
        if "/api/launcher/token/" in url:
            return {"ok": True}
        return poller_orig_fetch(url)

    poller_orig_fetch = poller._fetch_json
    poller.run_once()
    launched = pipeline.find("NEW LAUNCHER TOKEN (API)")
    assert launched
    # Any launchpad token is the whole point, CLOCKIN or not — it must be
    # delivered, not filtered as recon.
    assert launched[0]["category"] == "launch"
    assert launched[0]["level"] == "loud"
    assert not pipeline.find("*** CLOCKIN ***")
    assert state.is_tracked(BORING_ENTRY["address"])


def test_dedupe_holds_on_repeat(state, pipeline, errors):
    poller = make_poller(state, pipeline, errors, [TOKEN_ENTRY])
    poller.run_once()
    first_count = len(pipeline.sent)
    poller.run_once()
    poller.run_once()
    assert len(pipeline.sent) == first_count, "repeat polls must not re-alert"


def test_dedupe_survives_restart(tmp_path, pipeline, errors):
    path = str(tmp_path / "state.json")
    state1 = State(path=path)
    poller1 = make_poller(state1, pipeline, errors, [TOKEN_ENTRY])
    poller1.run_once()
    assert pipeline.find("*** CLOCKIN ***")

    # Simulate a restart: brand-new State loaded from disk.
    state2 = State(path=path)
    assert state2.api_token_seen(CA)
    assert state2.is_tracked(CA)
    count_before = len(pipeline.sent)
    poller2 = make_poller(state2, pipeline, errors, [TOKEN_ENTRY])
    poller2.run_once()
    assert len(pipeline.sent) == count_before, "no duplicate alerts after restart"


def test_highlights_slots_trigger_too(state, pipeline, errors):
    poller = make_poller(state, pipeline, errors, [],
                         highlights={"ok": True, "king": TOKEN_ENTRY,
                                     "top": None, "hot": None})
    poller.run_once()
    assert pipeline.find("*** CLOCKIN ***")


def test_ca_set_comparison_not_array_length(state, pipeline, errors):
    """Entries can be pre-staged then removed; a second, different CA must
    still alert even if the array length stays the same."""
    poller = make_poller(state, pipeline, errors, [TOKEN_ENTRY])
    poller.run_once()
    count = len(pipeline.sent)

    poller2 = make_poller(state, pipeline, errors, [BORING_ENTRY])
    poller2.run_once()
    assert len(pipeline.sent) > count, "new CA with same array length must alert"


def test_throttle_backoff_alerts_once(state, pipeline, errors):
    poller = ApiPoller(state, pipeline, errors)
    poller._fetch_json = lambda url: None  # every fetch fails
    poller.consecutive_throttles = 10
    poller.run_once()
    assert len(pipeline.find("API POLLER THROTTLED")) == 1
    poller.run_once()
    assert len(pipeline.find("API POLLER THROTTLED")) == 1, "alert exactly once"
    assert poller.interval() >= 30.0, "backs off to 30s while throttled"


def test_extract_ca_from_unknown_schema():
    assert extract_ca({"contractAddress": CA}) == CA
    assert extract_ca({"nested": {"whatever": f"token at {CA} yes"}}) == CA
    assert extract_ca({"name": "no address here"}) is None
    assert json.dumps(TOKEN_ENTRY)  # sanity: entries stay JSON-serializable
