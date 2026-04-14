"""External API clients for the launch + exploit detection pipeline.

Each client exposes a `configured` property. Detection modules check this
before calling — missing keys cause the module to log and skip rather
than crash the scheduler. This is deliberate: users may add API keys
gradually (Nansen paid, Helius free, etc.) and the pipeline must keep
running on whatever subset is available.
"""

from backend.clients.defillama import DeFiLlamaClient, defillama
from backend.clients.geckoterminal import GeckoTerminalClient, geckoterminal
from backend.clients.helius import HeliusClient, helius
from backend.clients.nansen import NansenClient, nansen
from backend.clients.gmgn import GMGNClient, gmgn
from backend.clients.birdeye import BirdeyeClient, birdeye

__all__ = [
    "DeFiLlamaClient", "defillama",
    "GeckoTerminalClient", "geckoterminal",
    "HeliusClient", "helius",
    "NansenClient", "nansen",
    "GMGNClient", "gmgn",
    "BirdeyeClient", "birdeye",
]
