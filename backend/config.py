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

    # Scoring thresholds
    ALERT_THRESHOLD: int = 70
    WATCHLIST_THRESHOLD: int = 50
    MAX_ALERTS_PER_DAY: int = 5

    # Scanner intervals (minutes)
    VOLUME_SCAN_INTERVAL: int = 15
    PROFILE_CHECK_INTERVAL: int = 60
    WALLET_ANALYZE_INTERVAL: int = 240
    EXCHANGE_FLOW_INTERVAL: int = 30
    SOCIAL_SCAN_INTERVAL: int = 120
    WALLET_TRACK_INTERVAL: int = 30
    SCORE_RECALC_INTERVAL: int = 30

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
