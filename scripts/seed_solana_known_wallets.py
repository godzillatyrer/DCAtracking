"""
Seed solana_known_wallets with a curated starter list.

Phase 3 ships with NO baked-in Solana operator addresses — speculative
guessing would put random wallets in the watchlist and pollute alerts.
The right way to populate this table is one of:

  1. Add wallets manually via POST /api/wallets/solana/add (when wired)
  2. Run with NANSEN_API_KEY set + a separate ingest script that pulls
     Nansen Smart Money labels for Solana
  3. Once your golden_deployers seeder grows beyond BSC, mirror the
     pattern: pull historical $50M+ Solana launches from DeFi Llama,
     resolve mint authority via Helius, store as role='golden_deployer'.

This script ensures the table exists and seeds a small list of
verifiable, public Solana protocol addresses (Raydium AMM, Pump.fun
program) tagged role='infrastructure' so they show up in the table but
don't trigger detection alerts (the detection paths only act on
wallets with role in WHALE_ROLES).
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.database import SessionLocal
from backend.models.solana_known_wallet import SolanaKnownWallet


# Minimal verifiable seed — public infrastructure programs. Tagged
# 'infrastructure' so they're excluded from whale-funding scans.
INFRASTRUCTURE = [
    ("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8", "Raydium AMM v4"),
    ("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  "Pump.fun program"),
    ("CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK", "Raydium CLMM"),
    ("9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP",  "Orca Whirlpools"),
    ("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",   "Jupiter v6"),
    ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   "SPL Token Program"),
]


def main():
    db = SessionLocal()
    added = 0
    try:
        for addr, name in INFRASTRUCTURE:
            existing = db.query(SolanaKnownWallet).filter_by(wallet_address=addr).first()
            if existing:
                continue
            db.add(SolanaKnownWallet(
                wallet_address=addr,
                label=name,
                role="infrastructure",
                is_active=True,
                added_at=datetime.utcnow(),
                notes="Public Solana infrastructure — excluded from whale scans.",
            ))
            added += 1
        db.commit()
        print(f"Done. Added {added} infrastructure rows. "
              f"Populate operator wallets via Nansen integration or manual /api/wallets/solana/add.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
