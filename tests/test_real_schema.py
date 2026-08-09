"""Behaviour pinned to the launcher's ACTUAL shapes, read from the live site.

Everything here was previously guesswork. The entry shape is
`response.tokens[i].token` — a top-level "token" field holding the address —
and the factory address is a build-time NEXT_PUBLIC_* injection that ships as
an empty string until the team sets it.
"""
from stonk_watcher import config
from stonk_watcher.api_poller import extract_ca
from stonk_watcher.frontend_diff import FrontendDiffer

CA = "0x1234567890abcdef1234567890abcdef12345678"
FACTORY = "0xfeed0000000000000000000000000000000beef1"

# Verbatim shape from page-561d9faec7e7c7e1.js:
#   r.tokens.filter(e => a.has(e.token.toLowerCase()))
REAL_ENTRY = {
    "token": CA, "symbol": "GMEOW", "name": "Gmeow",
    "quote": "0x0000000000000000000000000000000000000000",
    "pool": "0xaaaa000000000000000000000000000000000001",
    "graduated": False, "curvePct": 12.5, "mcapUsd": 4200.0,
    "creator": "0xbbbb000000000000000000000000000000000002",
}


def test_extract_ca_reads_the_real_field():
    assert extract_ca(REAL_ENTRY) == CA, (
        "the launcher uses `token`, not address/ca/contractAddress")


def test_extract_ca_prefers_token_over_other_addresses():
    """The entry also carries quote, pool and creator addresses. Picking any
    of those would alert the wrong contract as the launch."""
    ca = extract_ca(REAL_ENTRY)
    assert ca != REAL_ENTRY["pool"]
    assert ca != REAL_ENTRY["creator"]
    assert ca != REAL_ENTRY["quote"]


def test_detail_endpoint_shape_also_resolves():
    """/api/launcher/token/{addr} returns {ok, token:{...}} where `token` is
    the whole object — while inside it, `token` is the address string."""
    detail = {"ok": True, "token": REAL_ENTRY}
    assert extract_ca(detail) == CA


def test_quote_assets_are_never_launches():
    for addr in ("0x5fc5360d0400a0fd4f2af552add042d716f1d168",   # USDG
                 "0xe934e36a439c94017b64a3fece66af12099abf50",   # $STONKBROKER
                 "0x0bd7d308f8e1639fab988df18a8011f41eacad73"):  # WETH9
        assert addr in config.BORING_ADDRESSES


class FakeBlockscout:
    def classify_address(self, address):
        return {"exists": True, "is_contract": True, "token": None}


def make_differ(state, pipeline, errors, bundle):
    differ = FrontendDiffer(state, pipeline, errors, blockscout=FakeBlockscout())
    differ._fetch_text = lambda url: bundle
    return differ


# Verbatim from chunk 6716, module 2933, as it ships today.
UNSET_BUNDLE = (
    'chainId:Number("4663"),rpcUrl:"https://rpc.mainnet.chain.robinhood.com",'
    'tokenRegistry:o.env.NEXT_PUBLIC_STONK_TOKEN_REGISTRY_ADDRESS||"",'
    'launcherFactory:o.env.NEXT_PUBLIC_STONK_LAUNCHER_FACTORY_ADDRESS||"",'
    'launcherFactoryStartBlock:Number(o.env.NEXT_PUBLIC_LAUNCHER_START||"0"),'
)
# What ships once the env var is set: Next.js inlines the literal at build.
SET_BUNDLE = (
    f'chainId:Number("4663"),launcherFactory:"{FACTORY}",'
    'launcherFactoryStartBlock:Number("31200000"),'
)


def test_unset_factory_does_not_arm_anything(state, pipeline, errors):
    make_differ(state, pipeline, errors, UNSET_BUNDLE).run_once()
    assert not state.candidate_factories(), (
        'launcherFactory:"" must not be mistaken for an address')
    assert not pipeline.find("LAUNCHER FACTORY PUBLISHED")


def test_published_factory_is_armed_and_alerts_loudly(state, pipeline, errors):
    make_differ(state, pipeline, errors, SET_BUNDLE).run_once()

    assert FACTORY in state.candidate_factories()
    armed = state.candidate_factories()[FACTORY]
    assert armed["source"] == "frontend_config"
    assert armed["deploy_block"] == 31200000, "start block read from the bundle"
    alert = pipeline.find("LAUNCHER FACTORY PUBLISHED")
    assert alert and alert[0]["level"] == "loud"
    assert alert[0]["category"] == "launch"
    assert FACTORY in (alert[0]["code_lines"] or [])


def test_factory_alert_fires_on_the_very_first_cycle(state, pipeline, errors):
    """It must not be swallowed by the address baseline, which suppresses
    everything else on a cold start."""
    assert not state.frontend()["baselined"]
    make_differ(state, pipeline, errors, SET_BUNDLE).run_once()
    assert pipeline.find("LAUNCHER FACTORY PUBLISHED")


def test_factory_armed_only_once(state, pipeline, errors):
    differ = make_differ(state, pipeline, errors, SET_BUNDLE)
    differ.run_once()
    count = len(pipeline.find("LAUNCHER FACTORY PUBLISHED"))
    differ.run_once()
    assert len(pipeline.find("LAUNCHER FACTORY PUBLISHED")) == count


def test_frontend_cadence_tightens_near_t0():
    from datetime import timedelta
    t0 = config.get_t0()
    differ = FrontendDiffer.__new__(FrontendDiffer)
    assert differ.interval(t0 - timedelta(days=5)) == config.FRONTEND_POLL_INTERVAL
    assert differ.interval(t0 - timedelta(hours=6)) == 300.0
    assert differ.interval(t0 - timedelta(minutes=10)) == 60.0
    assert differ.interval(t0 + timedelta(hours=1)) == 60.0
