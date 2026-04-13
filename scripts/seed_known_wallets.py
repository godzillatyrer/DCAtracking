"""
Seed Known Wallets — One-time script to populate known insider wallets
from the 5 confirmed pump tokens.

Usage:
    python scripts/seed_known_wallets.py

Requires:
    - DATABASE_URL env var (or .env file)
    - BSCSCAN_API_KEY env var (or .env file)
"""

import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.database import Base, engine
from backend.trackers.wallet_seeder import run_wallet_seeder
from backend.models.exchange_wallet import ExchangeWallet
from backend.database import SessionLocal


# Known BSC exchange hot wallets to seed
EXCHANGE_HOT_WALLETS = [
    # Binance
    ("0x8894e0a0c962cb723c1ef8a1b5b5b174d81bd981", "Binance", "hot_wallet"),
    ("0xe2fc31f816a9b94326492132018c3aecc4a93ae1", "Binance", "hot_wallet"),
    ("0xf977814e90da44bfa03b6295a0616a897441acec", "Binance", "hot_wallet"),
    ("0x28c6c06298d514db089934071355e5743bf21d60", "Binance", "hot_wallet"),
    ("0x21a31ee1afc51d94c2efccaa2092ad1028285549", "Binance", "hot_wallet"),
    # Gate.io
    ("0x0d0707963952f2fba59dd06f2b425ace40b492fe", "Gate.io", "hot_wallet"),
    ("0x1c4b70a3968436b9a0a9cf5205c787eb81bb558c", "Gate.io", "hot_wallet"),
    # Bybit
    ("0xf89d7b9c864f589bbf53a82105107622b35eaa40", "Bybit", "hot_wallet"),
    # KuCoin
    ("0xd6216fc19db775df9774a6e33526131da7d19a2c", "KuCoin", "hot_wallet"),
    ("0x689c56aef474df92d44a1b70850f808488f9769c", "KuCoin", "hot_wallet"),
    # MEXC
    ("0x4982085c9e2f89f2ecb8131eca71afad896e89cb", "MEXC", "hot_wallet"),
    # Bitget
    ("0x97b9d2aa81164948c17c1beef8e3d5f3f6c1d8c5", "Bitget", "hot_wallet"),
    # OKX
    ("0x6cc5f688a315f3dc28a7781717a9a798a59fda7b", "OKX", "hot_wallet"),
]


def seed_exchange_wallets():
    """Seed the exchange_wallets table with known BSC hot wallets."""
    db = SessionLocal()
    added = 0
    try:
        for address, name, wallet_type in EXCHANGE_HOT_WALLETS:
            existing = db.query(ExchangeWallet).filter_by(wallet_address=address.lower()).first()
            if not existing:
                wallet = ExchangeWallet(
                    wallet_address=address.lower(),
                    exchange_name=name,
                    wallet_type=wallet_type,
                    chain="bsc",
                    verified=True,
                )
                db.add(wallet)
                added += 1
        db.commit()
        print(f"Seeded {added} exchange hot wallet addresses.")
    finally:
        db.close()


async def main():
    print("=" * 60)
    print("BSC Pump Scanner — Known Wallet Seeder")
    print("=" * 60)

    # Create tables
    print("\nCreating database tables...")
    Base.metadata.create_all(bind=engine)
    print("Done.")

    # Seed exchange wallets
    print("\nSeeding exchange hot wallets...")
    seed_exchange_wallets()

    # Seed known wallets from confirmed pumps
    print("\nSeeding known wallets from confirmed pump tokens...")
    print("This will trace deployers, top holders, and funding sources.")
    print("This may take several minutes due to BscScan rate limits.\n")
    await run_wallet_seeder()

    print("\nSeeding complete!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
