"""Central configuration for the Stonk Launcher / CLOCKIN watcher.

Values can be overridden with environment variables or a `.env` file in the
project root (simple KEY=VALUE lines; real environment variables win).
"""
import os
from datetime import datetime, timezone

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

TEAM_WALLETS = [
    "0xb668382cF44038a3E8140E789060F6A809787CDa",  # team wallet #1
    "0xBe498aad9c6fd0E4Cd6d1E3fBb395026c5D28215",  # team wallet #2 (USDG proxy deployer)
]
_extra_wallets = env("EXTRA_TEAM_WALLETS")
if _extra_wallets:
    TEAM_WALLETS += [w.strip() for w in _extra_wallets.split(",") if w.strip()]

# Optional: known AMM factory (e.g. AMMFactoryV2). When set, tracked-token
# LP/market detection watches this factory's logs for tracked CAs.
AMM_FACTORY_ADDRESS = env("AMM_FACTORY_ADDRESS", "")
AMM_FACTORY_START_BLOCK = env_int("AMM_FACTORY_START_BLOCK", 29738000)

# eth_getLogs chunk size (halved automatically on range errors).
LOG_CHUNK_SIZE = env_int("LOG_CHUNK_SIZE", 2000)

# Known-boring addresses ignored by the frontend differ.
BORING_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0xca11bde05977b3631167028862be2a173976ca11",  # multicall3
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
