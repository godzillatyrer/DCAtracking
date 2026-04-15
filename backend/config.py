import os
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

load_dotenv()


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "postgresql://scanner:password@localhost:5432/pump_scanner"

    # MegaNode / BSCTrace (free tier — sign up at nodereal.io)
    MEGANODE_API_KEY: str = ""
    MEGANODE_BASE_URL: str = "https://bsc-mainnet.nodereal.io/v1"

    # Anthropic Claude API
    ANTHROPIC_API_KEY: str = ""

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    # Arkham Intelligence (optional)
    ARKHAM_API_KEY: str = ""
    ARKHAM_BASE_URL: str = "https://api.arkhamintelligence.com"

    # Optional APIs
    TWITTER_BEARER_TOKEN: str = ""
    COINGECKO_API_KEY: str = ""

    # DEX Screener (no key needed)
    DEXSCREENER_BASE_URL: str = "https://api.dexscreener.com"

    # ─── Phase 2: launch detection + exploit watcher ───────────────────
    # DeFi Llama (no key needed)
    DEFILLAMA_BASE_URL: str = "https://api.llama.fi"
    DEFILLAMA_COINS_URL: str = "https://coins.llama.fi"

    # GeckoTerminal (no key needed)
    GECKOTERMINAL_BASE_URL: str = "https://api.geckoterminal.com/api/v2"

    # Helius (Solana RPC)
    HELIUS_API_KEY: str = ""
    HELIUS_BASE_URL: str = "https://mainnet.helius-rpc.com"

    # Nansen (Smart Money labels, launch watcher)
    NANSEN_API_KEY: str = ""
    # Nansen API: base URL is /api/v1 (the /api/beta namespace was
    # deprecated on 2025-10-01). All data endpoints are POST with a
    # JSON body and require the lowercase `apikey` header.
    # Docs: https://docs.nansen.ai/getting-started/api-structure-and-base-url
    NANSEN_BASE_URL: str = "https://api.nansen.ai/api/v1"
    # Nansen is credit-metered (observed: /smart-money/holdings = 5
    # credits/call). We self-limit to NANSEN_DAILY_CALL_CAP total Nansen
    # requests per calendar day so accidental bursts / diagnostic probes
    # can't drain the credit balance. The client refuses further calls
    # past the cap and logs the cap-hit — the watchers degrade to
    # "Arkham-only" gracefully.
    NANSEN_DAILY_CALL_CAP: int = 200

    # GMGN (Solana smart money tracker)
    GMGN_API_KEY: str = ""
    GMGN_BASE_URL: str = "https://gmgn.ai/defi/quotation/v1"

    # Birdeye (Solana token data)
    BIRDEYE_API_KEY: str = ""
    BIRDEYE_BASE_URL: str = "https://public-api.birdeye.so"

    # ─── Alert gating ─────────────────────────────────────────────────
    # Launch-detection scoring/alert thresholds
    LAUNCH_SCORE_S_TIER: int = 80   # S = auto-alert
    LAUNCH_SCORE_A_TIER: int = 60   # A = alert if AI confirmation passes
    LAUNCH_SCORE_B_TIER: int = 30   # B = watchlist only, no alert

    # A fresh wallet is one whose on-chain age is <= this at time of funding
    FRESH_WALLET_MAX_AGE_DAYS: int = 7
    FRESH_WALLET_MAX_NONCE: int = 3
    WHALE_FUNDING_MIN_USD: float = 50_000.0
    WHALE_FRESH_DEPLOY_WINDOW_HOURS: int = 72

    BIG_LP_MIN_USD: float = 50_000.0
    INSIDER_EARLY_BUYER_BLOCK_WINDOW: int = 50  # blocks after pair creation
    INSIDER_EARLY_BUYER_MIN_COUNT: int = 3       # ≥N known wallets in first N blocks

    PORTFOLIO_GATE_MIN_USD: float = 1_000_000.0

    # Exploit detection thresholds
    EXPLOIT_TVL_DROP_PCT: float = 30.0       # >30% drop
    EXPLOIT_TVL_DROP_MINUTES: int = 15       # in <15 minutes
    EXPLOIT_TVL_MIN_PROTOCOL_USD: float = 50_000_000.0  # only for protocols >$50M
    EXPLOIT_ABNORMAL_MINT_PCT: float = 5.0   # >5% of circulating in one tx
    EXPLOIT_BRIDGE_DRAIN_MIN_USD: float = 10_000_000.0

    # Scoring thresholds (legacy pump scanner — unchanged)
    ALERT_THRESHOLD: int = 70
    WATCHLIST_THRESHOLD: int = 50
    MAX_ALERTS_PER_DAY: int = 5
    # Exploit alerts bypass MAX_ALERTS_PER_DAY (time-critical & rare).
    MAX_EXPLOIT_ALERTS_PER_DAY: int = 10

    # Scanner intervals (minutes).
    # Cadences are tuned so the enrichment pipeline keeps up with the
    # ~580/hr raw-flag rate from the volume scanner.
    VOLUME_SCAN_INTERVAL: int = 15
    PROFILE_CHECK_INTERVAL: int = 30
    WALLET_ANALYZE_INTERVAL: int = 240
    EXCHANGE_FLOW_INTERVAL: int = 30
    SOCIAL_SCAN_INTERVAL: int = 120
    WALLET_TRACK_INTERVAL: int = 30
    SCORE_RECALC_INTERVAL: int = 30

    # Phase 2: launch + exploit intervals
    PAIR_WATCHER_INTERVAL_MIN: int = 5
    DEPLOYER_WATCHER_INTERVAL_MIN: int = 10
    WHALE_FRESH_WATCHER_INTERVAL_MIN: int = 10
    TREASURY_OUTFLOW_INTERVAL_MIN: int = 30
    LAUNCH_SCORER_INTERVAL_MIN: int = 2
    # Bumped 3→5 after a 10× stale observation — at 3min the run budget
    # was too tight once _check_bridge_drains ran across 6 bridges.
    EXPLOIT_WATCHER_INTERVAL_MIN: int = 5

    # Phase 3: Solana intervals
    SOLANA_PAIR_WATCHER_INTERVAL_MIN: int = 7
    SOLANA_DEPLOYER_WATCHER_INTERVAL_MIN: int = 12
    SOLANA_WHALE_FRESH_INTERVAL_MIN: int = 12

    # Wallet tracker dust filter — skip token transfers whose USD value
    # cannot be estimated to be above this threshold.
    WALLET_TRACK_MIN_USD: float = 500.0

    # Max wallets scanned per wallet_tracker run. With 500+ seeded known
    # wallets and ~10 RPC calls per wallet, scanning all of them in one
    # run exceeded the 30-min interval and caused the job to go stale.
    # 50/run × 30-min interval = rotate the full list roughly every 5-6 hours.
    WALLET_TRACK_BATCH_SIZE: int = 50

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
