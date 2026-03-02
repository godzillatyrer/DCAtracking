import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Helius RPC (free tier: 100K credits/day)
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

# Jupiter DCA Program on Solana mainnet
JUPITER_DCA_PROGRAM = "DCA265Vj8a9CEuX1eb1LWRnDT7uK6q1xMipnNyatn23M"

# Known base mints (input tokens for DCA orders)
KNOWN_BASE_MINTS = {
    "So11111111111111111111111111111111111111112": {"symbol": "SOL", "decimals": 9},
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": {"symbol": "USDC", "decimals": 6},
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": {"symbol": "USDT", "decimals": 6},
}

# Scanner settings
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "120"))
MAX_MARKET_CAP = float(os.getenv("MAX_MARKET_CAP", "50000000"))
MIN_DCA_VALUE_USD = float(os.getenv("MIN_DCA_VALUE_USD", "500"))
DCA_CLUSTER_WINDOW_HOURS = int(os.getenv("DCA_CLUSTER_WINDOW_HOURS", "24"))
DCA_CLUSTER_MIN_ORDERS = int(os.getenv("DCA_CLUSTER_MIN_ORDERS", "3"))
ALERT_COOLDOWN_HOURS = int(os.getenv("ALERT_COOLDOWN_HOURS", "4"))

# Jupiter Price API (free, no key needed)
JUPITER_PRICE_API = "https://api.jup.ag/price/v2"

# GeckoTerminal API (free, no key needed)
GECKO_TERMINAL_BASE_URL = "https://api.geckoterminal.com/api/v2"

# Database
DB_PATH = os.getenv("DB_PATH", "dca_tracker.db")
