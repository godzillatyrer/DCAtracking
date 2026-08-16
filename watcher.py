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


def _describe_ca(bs, state: State, ca: str) -> dict:
    """Resolve one contract's identity and deployer. No printing."""
    out = {"ca": ca, "error": None, "creator": None, "reasons": [],
           "name": "?", "symbol": "?", "supply": "?"}
    try:
        info = bs.classify_address(ca)
    except Exception as exc:
        out["error"] = f"lookup failed: {exc}"
        return out
    if not info.get("exists"):
        out["error"] = "no contract at this address on this chain"
        return out

    token = info.get("token") or {}
    out["name"] = str(token.get("name") or "?")
    out["symbol"] = str(token.get("symbol") or "?")
    out["supply"] = str(token.get("total_supply")
                        or token.get("totalSupply") or "?")
    try:
        creation = bs.creation_info(ca)
    except Exception as exc:
        out["error"] = f"creator lookup failed: {exc}"
        return out
    creator = (creation.get("creator") or "").lower()
    called = (creation.get("called_contract") or "").lower()
    out["deployer"] = creator or None
    out["called_contract"] = called or None
    if not creator and not called:
        out["error"] = "creation not indexed yet — re-run shortly"
        return out

    # The factory is whichever of the two is a contract. When a user calls
    # launchToken(), the token's recorded creator can be that caller's
    # WALLET while the factory is the contract the tx was sent to. Taking
    # "creator" at face value would label a real launchpad token as
    # hand-deployed, so the tx target wins when the creator is an EOA.
    creator_is_contract = None
    if creator:
        try:
            creator_is_contract = bs.classify_address(creator).get("is_contract")
        except Exception:
            creator_is_contract = None
    out["creator_is_contract"] = creator_is_contract
    if creator and creator_is_contract:
        out["creator"] = creator
        out["factory_from"] = "deployer of the token"
    elif called:
        out["creator"] = called
        out["factory_from"] = f"contract called by {creator or 'the creator'}"
    else:
        out["creator"] = creator
        out["factory_from"] = "deployer of the token"
    creator = out["creator"]

    known = config.KNOWN_STONK_FACTORIES.get(creator)
    if known:
        out["reasons"].append(f"creator is the known StonkBrokers {known}")
    armed = state.candidate_factories().get(creator)
    if armed:
        out["reasons"].append(
            f"creator is an armed factory (source: {armed.get('source')})")
        if armed.get("confirmed_launcher"):
            out["reasons"].append("creator is a CONFIRMED LauncherFactory")
    try:
        ident = bs.identify_launcher(creator)
        if ident.get("is_launcher"):
            out["reasons"].append(
                f"creator ABI exposes {', '.join(ident['markers'])}")
        out["creator_name"] = ident.get("name")
        out["factory_is_contract"] = bs.classify_address(creator).get("is_contract")
    except Exception:
        out["factory_is_contract"] = None
    return out


def cmd_check_ca(state: State, addresses) -> int:
    """Identify the deployer of one or more contracts.

    Given several tokens from the same launchpad, the deployer they share
    IS the factory — which is how the address is derived when it has never
    been published.
    """
    from stonk_watcher.alerts import is_target_token, verdict_for_target
    from stonk_watcher.blockscout import BlockscoutClient

    cleaned = []
    for raw in addresses:
        ca = raw.strip().lower().rstrip(",")
        if not (ca.startswith("0x") and len(ca) == 42):
            print(f"Not an address: {raw}")
            return 2
        cleaned.append(ca)

    bs = BlockscoutClient()
    results = []
    for ca in cleaned:
        print(f"=== {ca}")
        res = _describe_ca(bs, state, ca)
        results.append(res)
        if res["error"]:
            print(f"    {res['error']}\n")
            continue
        print(f"    token:   {res['name']} ({res['symbol']})")
        print(f"    supply:  {res['supply']}")
        if res.get("deployer") and res["deployer"] != res["creator"]:
            print(f"    tx sender:  {res['deployer']}  (the caller, not the factory)")
        print(f"    FACTORY:    {res['creator']}   [{res.get('factory_from')}]")
        if res.get("creator_name"):
            print(f"    verified as: {res['creator_name']}")
        if res.get("factory_is_contract") is False:
            print("    that address is a WALLET — hand-deployed, no factory")
        for reason in res["reasons"]:
            print(f"    - {reason}")
        if is_target_token(res["name"], res["symbol"]):
            verdict, note = verdict_for_target(res["supply"], res["symbol"],
                                               res["name"])
            print(f"    name matches {config.TARGET_TOKEN_NAME}: "
                  f"{verdict.upper()} — {note}")
        print()

    creators = {r["creator"] for r in results if r["creator"]}
    resolved = [r for r in results if r["creator"]]
    print("=" * 60)
    if not creators:
        print("VERDICT: could not resolve any creator. Nothing concluded.")
        return 3
    if len(creators) == 1 and len(resolved) > 1:
        factory = creators.pop()
        contract = all(r.get("factory_is_contract") is not False
                       for r in resolved)
        print(f"COMMON FACTORY across {len(resolved)} tokens:\n\n  {factory}\n")
        if not contract:
            print("But that creator is a WALLET, not a contract — these were")
            print("deployed by hand from one address, not by a factory.")
            return 1
        print("All of these tokens were deployed by that one contract, so it")
        print("is the launchpad factory. Arm it with:\n")
        print(f"  WATCH_CONTRACTS={factory}\n")
        print("Every future launch through it then alerts from chain.")
        return 0
    if len(resolved) > 1:
        print("DIFFERENT CREATORS — these did not all come from one factory:")
        for r in resolved:
            print(f"  {r['ca']} <- {r['creator']}")
        return 1
    single = resolved[0]
    if single["reasons"]:
        print("VERDICT: LAUNCHPAD TOKEN.")
        return 0
    print("VERDICT: UNCONFIRMED — creator is not a factory we recognise.")
    print(f"  Creator: {single['creator']}")
    return 1


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
    parser.add_argument("--check-ca", metavar="ADDRESS", nargs="+", default=None,
                        help="identify the deployer of one or more contracts; "
                             "a shared deployer across tokens IS the factory")
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
