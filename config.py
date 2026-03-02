import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Scanner settings
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))
MAX_MARKET_CAP = float(os.getenv("MAX_MARKET_CAP", "50000000"))
VOLUME_SPIKE_MULTIPLIER = float(os.getenv("VOLUME_SPIKE_MULTIPLIER", "5.0"))
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "5000"))
MIN_VOLUME_USD = float(os.getenv("MIN_VOLUME_USD", "1000"))

# DexScreener API (free, no key needed)
DEXSCREENER_BASE_URL = "https://api.dexscreener.com"

# Chains to monitor
CHAINS = ["solana", "ethereum", "bsc"]

# Database
DB_PATH = os.getenv("DB_PATH", "dca_tracker.db")
