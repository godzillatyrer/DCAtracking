#!/usr/bin/env python3
"""Stonk Launcher / CLOCKIN launch sniper — entrypoint.

Usage:
  python3 watcher.py               run the watcher (24/7)
  python3 watcher.py --test-alert  send a test alert on every channel
  python3 watcher.py --status      print state summary and poll cadence
  python3 watcher.py --check-ca 0x…  is this CA a launchpad launch?
  python3 watcher.py --once        run one cycle of each component and exit
  python3 watcher.py --port 10000  also serve a JSON health endpoint

The health endpoint is only needed on hosts that require an open port
(e.g. a Render web service). $PORT is honored automatically.
"""
import argparse
import logging
import sys
from datetime import datetime, timezone

from stonk_watcher import config
from stonk_watcher.alerts import AlertPipeline, ErrorReporter
from stonk_watcher.api_poller import ApiPoller
from stonk_watcher.chain_watcher import ChainWatcher
from stonk_watcher.frontend_diff import FrontendDiffer
from stonk_watcher.health import start_health_server
from stonk_watcher.state import State, as_checklist
from stonk_watcher.supervisor import Supervisor


def build_components(state, pipeline, errors):
    return [
        ApiPoller(state, pipeline, errors),
        ChainWatcher(state, pipeline, errors),
        FrontendDiffer(state, pipeline, errors),
    ]


def cmd_test_alert(pipeline: AlertPipeline) -> int:
    results = pipeline.send_test()
    ok = True
    for channel, result in results.items():
        print(f"  {channel:10s} {result}")
        if channel == "telegram" and result != "ok":
            ok = False
    if not ok:
        print("\nFAILED: Telegram is the primary channel and it did not deliver.")
        return 1
    print("\nAll configured channels delivered.")
    return 0


def cmd_check_ca(state: State, ca: str) -> int:
    """Answer 'is this CA a Stonk Launcher launch?' from on-chain provenance.

    Uses the same provenance rule the watcher enforces: what matters is who
    deployed the token, never what it is called or what its address looks
    like.
    """
    from stonk_watcher.alerts import is_target_token, verdict_for_target
    from stonk_watcher.blockscout import BlockscoutClient

    ca = ca.strip().lower()
    if not (ca.startswith("0x") and len(ca) == 42):
        print(f"Not an address: {ca}")
        return 2

    bs = BlockscoutClient()
    print(f"checking {ca}\n")
    try:
        info = bs.classify_address(ca)
    except Exception as exc:  # network/parse — report, do not pretend
        print(f"FAILED to reach Blockscout: {exc}")
        return 3
    if not info.get("exists"):
        print("VERDICT: NOT A CONTRACT ON THIS CHAIN — nothing deployed here.")
        return 1

    token = info.get("token") or {}
    name = str(token.get("name") or "?")
    symbol = str(token.get("symbol") or "?")
    supply = str(token.get("total_supply") or token.get("totalSupply") or "?")
    print(f"  token:    {name} ({symbol})")
    print(f"  supply:   {supply}")
    print(f"  contract: {info.get('is_contract')}")

    try:
        creation = bs.creation_info(ca)
    except Exception as exc:
        print(f"\nFAILED to read creator: {exc}")
        return 3
    creator = (creation.get("creator") or "").lower()
    if not creator:
        print("\nVERDICT: UNKNOWN — creator not indexed yet. Re-run shortly.")
        return 1
    print(f"  creator:  {creator}")

    reasons = []
    known = config.KNOWN_STONK_FACTORIES.get(creator)
    if known:
        reasons.append(f"creator is the known StonkBrokers {known}")
    armed = state.candidate_factories().get(creator)
    if armed:
        reasons.append(f"creator is an armed factory (source: {armed.get('source')})")
        if armed.get("confirmed_launcher"):
            reasons.append("creator is a CONFIRMED LauncherFactory by ABI")
    ident = bs.identify_launcher(creator)
    if ident.get("is_launcher"):
        reasons.append(f"creator ABI exposes {', '.join(ident['markers'])}")
    if ident.get("name"):
        print(f"  creator verified as: {ident['name']}")

    creator_info = bs.classify_address(creator)
    if not creator_info.get("is_contract"):
        print("\nVERDICT: NOT A LAUNCHPAD TOKEN.")
        print("  The creator is a plain wallet, so this was hand-deployed.")
        print("  A launchpad token is deployed BY the launchpad contract.")
        return 1

    print()
    if reasons:
        print("VERDICT: LAUNCHPAD TOKEN — deployed by the launchpad.")
        for reason in reasons:
            print(f"  - {reason}")
    else:
        print("VERDICT: UNCONFIRMED — deployed by a contract we cannot tie to")
        print("  the Stonk Launcher. It may be another launchpad or factory.")
        print(f"  Compare {creator} against the factory in your")
        print("  *** LAUNCHER FACTORY FOUND *** alert, if you have one.")

    if is_target_token(name, symbol):
        state_word, note = verdict_for_target(supply, symbol, name)
        print(f"\n  name matches {config.TARGET_TOKEN_NAME}: {state_word.upper()}")
        print(f"  {note}")
        print("  Duplicate tickers exist on this chain — the creator above is")
        print("  what decides, not the name.")
    if ca.endswith("666666"):
        print("\n  note: the ...666666 vanity suffix is the launcher's pattern,")
        print("  but anyone can mine it. It is not evidence either way.")
    return 0 if reasons else 1


def cmd_status(state: State) -> int:
    now = datetime.now(timezone.utc)
    t0 = config.get_t0()
    dt = (t0 - now).total_seconds()
    print(f"now: {now.isoformat()}")
    print(f"T0:  {t0.isoformat()}  ({dt / 3600:+.1f}h)")
    print(f"api poll interval:   {config.api_poll_interval(now, t0):.0f}s")
    print(f"chain poll interval: {config.chain_poll_interval(now, t0):.0f}s")
    print(f"telegram configured: {bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)}")
    print(f"rpc url: {config.RPC_URL}")
    for line in as_checklist(state):
        print(f"  {line}")
    tracked = state.data["tracked"]
    if tracked:
        print("tracked CAs:")
        for ca, info in tracked.items():
            print(f"  {ca}  [{info.get('source')}] {info.get('name', '?')} "
                  f"({info.get('symbol', '?')})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-alert", action="store_true",
                        help="send a test alert to all channels and exit")
    parser.add_argument("--status", action="store_true",
                        help="print state summary and exit")
    parser.add_argument("--once", action="store_true",
                        help="run one cycle of each component and exit")
    parser.add_argument("--check-ca", metavar="ADDRESS", default=None,
                        help="is this contract a Stonk Launcher launch?")
    parser.add_argument("--port", type=int, default=None,
                        help="serve a JSON health endpoint on this port "
                             "(defaults to $PORT when set)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S")

    pipeline = AlertPipeline()
    if args.test_alert:
        return cmd_test_alert(pipeline)

    state = State()
    if args.check_ca:
        return cmd_check_ca(state, args.check_ca)
    if args.status:
        return cmd_status(state)

    errors = ErrorReporter(pipeline)
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        print("WARNING: Telegram is not configured (TELEGRAM_BOT_TOKEN / "
              "TELEGRAM_CHAT_ID). Alerts will only be logged to the console.",
              file=sys.stderr)

    components = build_components(state, pipeline, errors)
    if args.once:
        for component in components:
            print(f"-- running {component.name} once")
            component.run_once()
        state.save()
        return 0

    supervisor = Supervisor(state, pipeline, errors)
    for component in components:
        supervisor.add(component)

    port = args.port if args.port is not None else config.env_int("PORT", 0)
    if port:
        start_health_server(supervisor, port)

    supervisor.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
