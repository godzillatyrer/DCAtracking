"""Central configuration for the Stonk Launcher / CLOCKIN watcher.

Values can be overridden with environment variables or a `.env` file in the
project root (simple KEY=VALUE lines; real environment variables win).
"""
import os
from datetime import datetime, timezone
from typing import Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv() -> None:
    path = os.path.join(PROJECT_ROOT, ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


_load_dotenv()


def env(key: str, default: str = "") -> str:
    """Read an env var, treating blank/whitespace-only as absent.

    Hosting dashboards (Render's blueprint form among them) create the
    variable with an empty value when you leave an optional field blank, so a
    plain os.environ.get(key, default) would return "" and silently discard
    the default — e.g. blanking RPC_URL would point the RPC client at an
    empty URL and kill on-chain detection with no obvious cause.

    Values are stripped because those dashboard fields are textareas: a
    pasted bot token can carry a trailing newline, which would otherwise be
    baked straight into the Telegram API URL.
    """
    value = os.environ.get(key)
    if value is None or not value.strip():
        return default
    return value.strip()


def env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "").strip() or default)
    except ValueError:
        return default


# --- Launch event -----------------------------------------------------------
# Stonk Launcher goes live Tue Aug 11 8:00 PM EST = Wed Aug 12 00:00 UTC.
# Override with T0_UTC=2026-08-12T00:00:00Z (ISO 8601, UTC assumed if naive).
DEFAULT_T0 = datetime(2026, 8, 12, 0, 0, 0, tzinfo=timezone.utc)


def get_t0() -> datetime:
    raw = env("T0_UTC")
    if not raw:
        return DEFAULT_T0
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return DEFAULT_T0


TARGET_TOKEN_NAME = env("TARGET_TOKEN_NAME", "CLOCKIN")
# Ticker collisions are severe on this chain: six CLOCKIN variants already
# exist on the StonkBrokers factory (supplies 1e6 to 1e9) plus a squat on
# pools.fun. A symbol match alone therefore confirms nothing.
#
# Per-symbol expected total supply, so several tokens can be graded at once.
# A symbol with a known supply is graded CONFIRMED or CANDIDATE - SPEC
# MISMATCH; one without stays CANDIDATE rather than being trusted blindly.
# Extend with EXPECTED_SUPPLIES=SYM:123,OTHER:456 (env entries win).
EXPECTED_SUPPLIES = {
    # Confirmed by the user. Note the TICKERYARD test deploy at
    # 0x996cbbA6d831D58842C63EdF0dbc625F3f67Df9B carries 10,000,000,000 and
    # is NOT the live token — this is exactly what the grading catches.
    "YARD": "1999999980",
}
for _pair in env("EXPECTED_SUPPLIES").split(","):
    if ":" in _pair:
        _sym, _, _sup = _pair.partition(":")
        if _sym.strip() and _sup.strip():
            EXPECTED_SUPPLIES[_sym.strip().lstrip("$").upper()] = _sup.strip()

# Fallback applied when the symbol has no entry above.
EXPECTED_SUPPLY = env("EXPECTED_SUPPLY")


def expected_supply_for(symbol: Optional[str] = None,
                        name: Optional[str] = None) -> str:
    """Expected supply for a token, by symbol then name, else the fallback."""
    for value in (symbol, name):
        if not value:
            continue
        key = str(value).strip().lstrip("$").upper()
        if key in EXPECTED_SUPPLIES:
            return EXPECTED_SUPPLIES[key]
    return EXPECTED_SUPPLY

# What reaches your phone.
#   "launches" (default) — only actual token launches, plus watcher-health
#                          messages so a broken watcher is still visible.
#   "all"                — every reconnaissance signal too: team wallet
#                          activity, factory deploys, frontend diffs.
# Recon alerts are still written to the log in "launches" mode; they are
# simply not sent. Detection is unaffected either way — this filters
# delivery only, so a suppressed factory deploy is still armed internally.
ALERTS_MODE = env("ALERTS_MODE", "launches").lower()

# --- Stonk Launcher backend -------------------------------------------------
API_BASE = env("STONK_API_BASE", "https://www.stonkbrokers.cash")
API_TOKENS_URL = API_BASE + "/api/launcher/tokens"
API_HIGHLIGHTS_URL = API_BASE + "/api/launcher/highlights"
API_TOKEN_DETAIL_URL = API_BASE + "/api/launcher/token/{ca}"
LAUNCHER_PAGE_URL = API_BASE + "/launcher"

BROWSER_USER_AGENT = env(
    "WATCHER_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
)

# --- Robinhood Chain --------------------------------------------------------
BLOCKSCOUT_BASE = env("BLOCKSCOUT_BASE", "https://robinhoodchain.blockscout.com")
# Blockscout ships a JSON-RPC proxy at /api/eth-rpc which supports
# eth_blockNumber / eth_getLogs; set RPC_URL to a real node endpoint if you
# have one (lower latency, higher limits).
RPC_URL = env("RPC_URL", BLOCKSCOUT_BASE + "/api/eth-rpc")

# The RPC rejects the default Python/requests User-Agent with 403. A plain
# curl UA is what it accepts — using the browser UA here is not safe, and a
# 403 would silently blind the entire on-chain path.
RPC_USER_AGENT = env("RPC_USER_AGENT", "curl/8.5.0")
# Robinhood Chain produces ~250ms blocks (~14,400/hour), so block-count
# windows are much shorter in wall-clock terms than on a 12s chain.
BLOCKS_PER_HOUR = 14_400
# eth_getLogs hard limit on this RPC is 10,000 blocks per call.
MAX_LOG_WINDOW = 10_000

TEAM_WALLETS = [
    "0xb668382cF44038a3E8140E789060F6A809787CDa",  # MASTER EOA; deployed the token factory
    "0xBe498aad9c6fd0E4Cd6d1E3fBb395026c5D28215",  # team wallet #2 (USDG proxy deployer)
]

# Confirmed on-chain StonkBrokers factories, armed at startup. The
# CollectionTokenDeployer has already created 40 tokens (including several
# CLOCKIN variants), so it is a real launch source, not a guess.
KNOWN_STONK_FACTORIES = {
    "0x662003bf6049e36b4e887d47b8df8718ffbbc6c2": "CollectionTokenDeployer",
    # The Stonk Launcher is a PAIR of contracts, confirmed on-chain from the
    # STONKS / STONKCAT / BROKE deploys. Both are armed because they play
    # different roles and only one of them emits the launch events.
    "0x80a77001456bc986083678f9a112b1ec2aa07281": "StonkLauncher factory (entrypoint)",
    "0x00f8c29b28cb00a20f0ca071efaed0d3fe15dd97": "StonkLaunchDeployer (CREATE2)",
}

# Contracts that together constitute the launchpad. A token deployed by ANY
# of these is a launchpad token: the entrypoint receives launchToken() and
# delegates the actual CREATE2 to the deployer, which was split out to keep
# the factory under the EIP-170 24KB limit. So Blockscout records the
# DEPLOYER as a token's creator while the ENTRYPOINT is what emits events —
# and requiring creator == the emitting factory would reject every genuine
# launch.
STONK_LAUNCHPAD_CONTRACTS = {
    "0x80a77001456bc986083678f9a112b1ec2aa07281",
    "0x00f8c29b28cb00a20f0ca071efaed0d3fe15dd97",
}
for _extra in env("EXTRA_LAUNCHPAD_CONTRACTS").split(","):
    _addr = _extra.strip().lower()
    if _addr.startswith("0x") and len(_addr) == 42:
        STONK_LAUNCHPAD_CONTRACTS.add(_addr)
        KNOWN_STONK_FACTORIES.setdefault(_addr, "configured launchpad contract")

# Every launcher token so far carries exactly this supply (1e9 with 18
# decimals), which is the factory's defaultTotalSupply(). Corroborating, not
# decisive — a launch can override it.
LAUNCHER_DEFAULT_SUPPLY = "1000000000000000000000000000"

# Safety Deposit Box lockers. An LP lock is the strongest launch signal on
# this ecosystem: it means liquidity was just committed for a token.
LP_LOCKERS = {
    "0xfc96cf67ecc55be4adabc3aecbe6ad6349f11223": "StonkLiquidityLocker (V3)",
    "0x5a28ce098750f73bc9ec142d4bce464e1a0bbda6": "StonkLiquidityLockerV4",
}
# keccak-verified topic0s — do not recompute.
TOPIC_POSITION_LOCKED_V3 = (
    "0x7a5a16b84333b2656a94dfb32929b9b2b41facdc932c3cf70567b803edc92b8f")
TOPIC_POSITION_LOCKED_V4 = (
    "0xb7a1a7a5e8caa86bcc670a4b0a554f0ca0aeae608277451860c6dff33b574e54")
TOPIC_LOCK_FEES_COLLECTED = (
    "0x53d5968f2c4070d6fd41f8ac74ba6abeb336da7c5730caa9383551abc20aa15e")
TOPIC_POOL_CREATED_V3 = (
    "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118")
LP_LOCK_TOPICS = {TOPIC_POSITION_LOCKED_V3, TOPIC_POSITION_LOCKED_V4}

# A contract exposing these is the LauncherFactory, by its own ABI. This is
# a positive identification, not a heuristic.
LAUNCHER_ABI_MARKERS = ("createLaunch", "finalizeLaunch", "launchToken")

# pools.fun is a SEPARATE ecosystem sharing the chain — 196 tokens in 85
# minutes, one wallet minting 52 of them at 2-3s intervals. Nothing from it
# is a StonkBrokers launch, and conflating the two is what produced the
# earlier alert flood.
PARTY_FACTORY = "0x626c3d09b65bf5d1d40e0d5f25e19fa49783b3d4"
PARTY_SWAP_ROUTER = "0xe01020e83257bab1833d1ce052c572fcbcbf0cb8"

# Names that mark a deliberate test deploy; the team has shipped several.
TEST_TOKEN_PATTERN = env("TEST_TOKEN_PATTERN", r"test|tstdonotbuy|donotbuy|scram")

# --- Liquidity seeding (up. DEX and any other venue) ------------------------
# Launchpad tokens are seeded for LP on up. (up33.xyz) after bonding, but
# up.'s factory address is unpublished. Rather than guess it, liquidity is
# detected from the TOKEN's side: watch Transfer events emitted by tokens we
# already confirmed are launchpad tokens, and flag the first transfer into a
# contract. That contract is the pool. This is venue-agnostic — it works for
# up., Uniswap V3, or anything else — and stays provenance-safe because only
# already-confirmed launchpad tokens are ever watched.
LIQUIDITY_WATCH = env_bool("LIQUIDITY_WATCH", True)
# ERC-20 Transfer(address indexed from, address indexed to, uint256)
TRANSFER_TOPIC0 = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")
# Recipients that are plumbing rather than a pool.
NON_POOL_RECIPIENTS = set(LP_LOCKERS) | {
    "0x73991a25c818bf1f1128deaab1492d45638de0d3",  # V3 PositionManager
    "0x58daec3116aae6d93017baaea7749052e8a04fa7",  # V4 PositionManager
    "0x8366a39cc670b4001a1121b8f6a443a643e40951",  # V4 PoolManager
}
# Labels for pool-deploying factories, so an alert can name the venue. up.'s
# address is unknown; when a pool's creator is unrecognised the alert reports
# that address so the venue is learned from the first real graduation.
KNOWN_DEX_LABELS = {
    "0x1f7d7550b1b028f7571e69a784071f0205fd2efa": "Uniswap V3",
}
for _pair in env("UP_DEX_CONTRACTS").split(","):
    _addr = _pair.strip().lower()
    if _addr.startswith("0x") and len(_addr) == 42:
        KNOWN_DEX_LABELS[_addr] = "up. (up33.xyz)"
UP_DEX_URL = env("UP_DEX_URL", "https://up33.xyz")
MAX_POOL_CHECKS_PER_CYCLE = env_int("MAX_POOL_CHECKS_PER_CYCLE", 20)
_extra_wallets = env("EXTRA_TEAM_WALLETS")
if _extra_wallets:
    TEAM_WALLETS += [w.strip() for w in _extra_wallets.split(",") if w.strip()]

# Optional: known AMM factory (e.g. AMMFactoryV2). When set, tracked-token
# LP/market detection watches this factory's logs for tracked CAs.
AMM_FACTORY_ADDRESS = env(
    "AMM_FACTORY_ADDRESS", "0x1f7d7550b1b028f7571e69a784071f0205fd2efa")
# ~85 minutes of history at 250ms blocks; 0 means "recent window from head".
AMM_FACTORY_START_BLOCK = env_int("AMM_FACTORY_START_BLOCK", 0)

# eth_getLogs chunk size (halved automatically on range errors).
LOG_CHUNK_SIZE = min(env_int("LOG_CHUNK_SIZE", 5000), 10_000)

# Extra contracts to watch topic-agnostically, beyond those a team wallet is
# seen deploying. Add the launcher factory here the moment you learn its
# address (from the site, an announcement, or a NEW CONTRACT IN FRONTEND
# alert) — the watcher picks it up on the next cycle with no restart needed.
WATCH_CONTRACTS = [a.strip() for a in env("WATCH_CONTRACTS").split(",") if a.strip()]
# How far back to scan when a contract is added to WATCH_CONTRACTS.
WATCH_CONTRACTS_LOOKBACK = env_int("WATCH_CONTRACTS_LOOKBACK", 20000)

# Known-boring addresses ignored by the frontend differ. These are universal
# infrastructure deployed at the same address on every chain — their presence
# in a site bundle says nothing about a launch.
BORING_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",  # zero address
    "0x000000000000000000000000000000000000dead",  # burn address
    "0xca11bde05977b3631167028862be2a173976ca11",  # multicall3
    "0x0000000071727de22e5e9d8baf0edac6f37da032",  # ERC-4337 EntryPoint v0.7
    "0x5ff137d4b0fdcd49dca30c7cf57e578a026d2789",  # ERC-4337 EntryPoint v0.6
    "0x000000000022d473030f116ddee9f6b43ac78ba3",  # Permit2
    "0x4e59b44847b379578588920ca78fbf26c0b4956c",  # deterministic deployer
    "0x00000000000000adc04c56bf30ac9d3c0aaf14dc",  # Seaport 1.5
    # Stonk Launcher quote/pair assets, verified from the site bundle. The
    # factory indexes these in its logs constantly; they are never launches.
    "0x5fc5360d0400a0fd4f2af552add042d716f1d168",  # USDG
    "0xe934e36a439c94017b64a3fece66af12099abf50",  # $STONKBROKER
    "0x0bd7d308f8e1639fab988df18a8011f41eacad73",  # WETH9
    "0x55642a3f10f1af5145d3d59021b1d6b03bb8692c",  # Clock In fee router
    "0x57c0e45cb534413d1c20a4240955d6bb250bb4f1",  # $UP (up. DEX token)
    # pools.fun — a separate ecosystem on the same chain, not StonkBrokers.
    "0x626c3d09b65bf5d1d40e0d5f25e19fa49783b3d4",  # PartyFactory
    "0xe01020e83257bab1833d1ce052c572fcbcbf0cb8",  # PartySwapRouter
    # Testnet contracts the docs explicitly warn against using on mainnet.
    "0x631f9371fd6b2c85f8f61d19a90547ee67fa61a2",  # testnet LauncherFactory
    "0xfeccb63cd759d768538458ea56f47ea8004323c1",  # testnet V3 factory
    "0x37e402b8081efce1d82a09a066512278006e4691",  # testnet WETH9
}

# --- Alert channels ---------------------------------------------------------
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID")

TWILIO_ACCOUNT_SID = env("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = env("TWILIO_AUTH_TOKEN")
TWILIO_FROM = env("TWILIO_FROM")
TWILIO_TO = env("TWILIO_TO")

MACOS_ALERTS = env_bool("MACOS_ALERTS", False)

# --- Error reporting / watchdog --------------------------------------------
# Identical errors are re-alerted at most once per this window (seconds).
ERROR_ALERT_COOLDOWN = env_int("ERROR_ALERT_COOLDOWN", 600)
# Consecutive failures before a channel-down alert fires.
FAILURE_ALERT_THRESHOLD = env_int("FAILURE_ALERT_THRESHOLD", 5)
# Telegram "watcher alive" heartbeat interval in hours (0 disables).
HEARTBEAT_HOURS = env_int("HEARTBEAT_HOURS", 6)

STATE_FILE = env("STATE_FILE", os.path.join(PROJECT_ROOT, "data", "state.json"))


# --- Poll cadence -----------------------------------------------------------

def seconds_to_t0(now: datetime, t0: datetime) -> float:
    return (t0 - now).total_seconds()


def api_poll_interval(now: datetime, t0: datetime) -> float:
    """Track 1 schedule: 10s far out, 5s inside 24h, 2s in the war room
    (1h before T0 until 2h after), 10s afterwards."""
    dt = seconds_to_t0(now, t0)
    if dt > 24 * 3600:
        return 10.0
    if dt > 3600:
        return 5.0
    if dt > -2 * 3600:
        return 2.0
    return 10.0


def chain_poll_interval(now: datetime, t0: datetime) -> float:
    """Track 2c schedule: main loop 20s, tightening to 5s in the war room."""
    dt = seconds_to_t0(now, t0)
    if 3600 >= dt > -2 * 3600:
        return 5.0
    return 20.0


FRONTEND_POLL_INTERVAL = float(env_int("FRONTEND_POLL_INTERVAL", 3600))

BLOCKSCOUT_TOKEN_URL = BLOCKSCOUT_BASE + "/token/{ca}"
BLOCKSCOUT_TX_URL = BLOCKSCOUT_BASE + "/tx/{tx}"
BLOCKSCOUT_ADDRESS_URL = BLOCKSCOUT_BASE + "/address/{addr}"
