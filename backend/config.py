from pydantic_settings import BaseSettings
from dotenv import load_dotenv

load_dotenv()


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "postgresql://scanner:password@localhost:5432/pump_scanner"

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    # Helius (Solana RPC)
    HELIUS_API_KEY: str = ""
    HELIUS_BASE_URL: str = "https://mainnet.helius-rpc.com"

    # GMGN (Solana smart money — free tier)
    GMGN_API_KEY: str = ""
    GMGN_BASE_URL: str = "https://gmgn.ai/defi/quotation/v1"

    # Birdeye (Solana token data — free tier)
    BIRDEYE_API_KEY: str = ""
    BIRDEYE_BASE_URL: str = "https://public-api.birdeye.so"

    # GeckoTerminal (no key needed — public)
    GECKOTERMINAL_BASE_URL: str = "https://api.geckoterminal.com/api/v2"

    # DeFi Llama (no key needed — used for SOL price)
    DEFILLAMA_BASE_URL: str = "https://api.llama.fi"
    DEFILLAMA_COINS_URL: str = "https://coins.llama.fi"

    # DEX Screener (no key needed — pump.fun price fallback)
    DEXSCREENER_BASE_URL: str = "https://api.dexscreener.com"

    # Alert throttling
    MAX_ALERTS_PER_DAY: int = 10

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
