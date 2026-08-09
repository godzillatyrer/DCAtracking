"""Track 3 noise control.

The signal is an address that appears in the bundle *after* we start
watching. On a cold start every pre-existing RWA/stock token, WETH and piece
of infra is "new", which floods the channel and would bury the launch alert.
"""
from stonk_watcher import config
from stonk_watcher.frontend_diff import FrontendDiffer
from stonk_watcher.state import State

EXISTING_TOKEN = "0x05b37fb53a299a1b874a619e1c4c404d52c36f4c"   # RDDT
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
ENTRYPOINT = "0x0000000071727de22e5e9d8baf0edac6f37da032"       # ERC-4337
FACTORY = "0xfeed00000000000000000000000000000000beef"
NEW_TOKEN = "0xc10c1c10c1c10c1c10c1c10c1c10c1c10c1c10c1"


def make_differ(state, pipeline, errors, addresses, tokens=None):
    differ = FrontendDiffer(state, pipeline, errors, blockscout=FakeBlockscout(tokens))
    html = "".join(f'<script>const a="{a}";</script>' for a in addresses)
    differ._fetch_text = lambda url: html
    return differ


class FakeBlockscout:
    def __init__(self, tokens=None):
        self.tokens = tokens or {}
        self.calls = []

    def classify_address(self, address):
        self.calls.append(address)
        token = self.tokens.get(address.lower())
        if token is not None:
            return {"exists": True, "is_contract": True, "token": token}
        return {"exists": True, "is_contract": True, "token": None}


def test_first_run_baselines_silently(state, pipeline, errors):
    differ = make_differ(state, pipeline, errors,
                         [EXISTING_TOKEN, WETH, ENTRYPOINT])
    differ.run_once()

    assert not pipeline.find("NEW CONTRACT IN FRONTEND"), (
        "a cold start must not alert on pre-existing addresses")
    baseline = pipeline.find("FRONTEND BASELINED")
    assert baseline and baseline[0]["level"] == "info"
    assert differ.blockscout.calls == [], "baseline needs no Blockscout lookups"
    assert state.frontend()["baselined"]
    for addr in (EXISTING_TOKEN, WETH, ENTRYPOINT):
        assert addr in state.frontend()["seen_addresses"]


def test_address_appearing_after_baseline_alerts(state, pipeline, errors):
    make_differ(state, pipeline, errors, [EXISTING_TOKEN, WETH]).run_once()
    count = len(pipeline.sent)

    # A non-token contract shows up later — this is the factory signal.
    make_differ(state, pipeline, errors, [EXISTING_TOKEN, WETH, FACTORY]).run_once()
    alerts = pipeline.find("NEW CONTRACT IN FRONTEND")
    assert len(pipeline.sent) > count
    assert alerts and alerts[0]["level"] == "loud"
    assert FACTORY in alerts[0]["text"]


def test_new_listing_of_ordinary_token_is_quiet(state, pipeline, errors):
    make_differ(state, pipeline, errors, [WETH]).run_once()
    differ = make_differ(state, pipeline, errors, [WETH, NEW_TOKEN],
                         tokens={NEW_TOKEN: {"name": "Tesla", "symbol": "TSLA"}})
    differ.run_once()
    assert not pipeline.find("NEW CONTRACT IN FRONTEND"), (
        "ordinary token listings are Track 1/2's job, not a factory signal")
    quiet = pipeline.find("new token listed on site")
    assert quiet and quiet[0]["level"] == "info"


def test_clockin_token_in_bundle_is_loud(state, pipeline, errors):
    make_differ(state, pipeline, errors, [WETH]).run_once()
    differ = make_differ(state, pipeline, errors, [WETH, NEW_TOKEN],
                         tokens={NEW_TOKEN: {"name": "CLOCKIN", "symbol": "CLOCKIN"}})
    differ.run_once()
    alerts = pipeline.find("*** CLOCKIN *** IN FRONTEND BUNDLE")
    assert alerts and alerts[0]["level"] == "loud"


def test_known_infra_never_alerts(state, pipeline, errors):
    make_differ(state, pipeline, errors, [WETH]).run_once()
    differ = make_differ(state, pipeline, errors, [WETH, ENTRYPOINT])
    differ.run_once()
    assert ENTRYPOINT not in differ.blockscout.calls, (
        "universal infra is filtered before any lookup")
    assert not pipeline.find(ENTRYPOINT)


def test_baseline_survives_restart(tmp_path, pipeline, errors):
    path = str(tmp_path / "state.json")
    state1 = State(path=path)
    make_differ(state1, pipeline, errors, [EXISTING_TOKEN, WETH]).run_once()

    state2 = State(path=path)
    assert state2.frontend()["baselined"], "baseline must survive a restart"
    count = len(pipeline.sent)
    make_differ(state2, pipeline, errors, [EXISTING_TOKEN, WETH]).run_once()
    assert len(pipeline.sent) == count, "no re-baseline, no repeat alerts"


def test_entrypoint_is_in_boring_list():
    assert ENTRYPOINT in config.BORING_ADDRESSES
