"""
Seed known bridge contracts on BSC for Module 14 (bridge drain detection).

These are the BSC sides of major cross-chain bridges. Anomalous outflows
from these contracts are how bridge exploits manifest on-chain.

Run:  python scripts/seed_known_bridges.py
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.database import SessionLocal
from backend.models.exchange_wallet import ExchangeWallet


KNOWN_BRIDGES = [
    # (address, bridge_name)
    ("0x3ee18b2214aff97000d974cf647e54347f24cd7c", "Wormhole"),
    ("0xb6d053e260d410eac02ea28755696f90a8ecca2b", "Multichain"),   # legacy; deprecated
    ("0x5abfec25f74cd88437631a7731906932776356f9", "Stargate"),
    ("0x35bfb3d0e4b5347afba9db9ab89fb40dd2bafe84", "cBridge (Celer)"),
    ("0x88dca0ca6b3d8a33b8f0ed7e18e56b3a27fa5b43", "Synapse"),
    ("0xb5fe9e59fa13e44c8d14eeb7d00a9fa3d2b97e0f", "Polkadot XCM-bridge (BSC side)"),
]


def main():
    db = SessionLocal()
    added = 0
    try:
        for addr, name in KNOWN_BRIDGES:
            addr = addr.lower()
            existing = db.query(ExchangeWallet).filter_by(wallet_address=addr).first()
            if existing:
                if existing.wallet_type != "bridge":
                    existing.wallet_type = "bridge"
                if not existing.exchange_name:
                    existing.exchange_name = name
                continue
            row = ExchangeWallet(
                wallet_address=addr,
                exchange_name=name,
                wallet_type="bridge",
                chain="bsc",
                verified=True,
                added_at=datetime.utcnow(),
            )
            db.add(row)
            added += 1
            print(f"  + {name:30s} {addr}")
        db.commit()
        print(f"Done. Added {added} new bridge contracts.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
