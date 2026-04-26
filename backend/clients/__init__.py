"""External API clients for the Solana cabal tracker.

Each client exposes a `configured` property. Modules check this before
calling so missing keys log-and-skip rather than crash.
"""

from backend.clients.defillama import DeFiLlamaClient, defillama
from backend.clients.geckoterminal import GeckoTerminalClient, geckoterminal
from backend.clients.helius import HeliusClient, helius
from backend.clients.hyperliquid import HyperliquidClient, hyperliquid
from backend.clients.gmgn import GMGNClient, gmgn
from backend.clients.birdeye import BirdeyeClient, birdeye

__all__ = [
    "DeFiLlamaClient", "defillama",
    "GeckoTerminalClient", "geckoterminal",
    "HeliusClient", "helius",
    "HyperliquidClient", "hyperliquid",
    "GMGNClient", "gmgn",
    "BirdeyeClient", "birdeye",
]
