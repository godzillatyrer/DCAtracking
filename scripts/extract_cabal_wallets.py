"""
Extract cabal wallets from a Solana runner CA.

Paste the mint address of a token that just pumped → extracts early
profitable wallets and adds them to solana_known_wallets so the
detection pipeline starts tracking them.

Usage:
  python scripts/extract_cabal_wallets.py Hon2rHAiqkcDtUzL5gA2vjXPr7T1MPCK2UT2AHKCpump
  python scripts/extract_cabal_wallets.py Hon2rHAiqkcDtUzL5gA2vjXPr7T1MPCK2UT2AHKCpump --symbol BONK --min-profit 10000
  python scripts/extract_cabal_wallets.py Hon2rHAiqkcDtUzL5gA2vjXPr7T1MPCK2UT2AHKCpump --no-add  # just show, don't add
"""

import asyncio
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main():
    parser = argparse.ArgumentParser(description="Extract cabal wallets from a Solana runner")
    parser.add_argument("mint", help="Solana token mint address (CA)")
    parser.add_argument("--symbol", default="", help="Token symbol (for labeling)")
    parser.add_argument("--min-profit", type=float, default=5000, help="Min profit USD (default 5000)")
    parser.add_argument("--min-mult", type=float, default=3.0, help="Min profit multiplier (default 3x)")
    parser.add_argument("--max", type=int, default=30, help="Max wallets to extract (default 30)")
    parser.add_argument("--no-add", action="store_true", help="Don't add to DB, just display")
    args = parser.parse_args()

    from backend.detection.cabal_extractor import extract_cabal_wallets, add_cabal_wallets_to_db
    from backend.database import SessionLocal

    print(f"\nExtracting cabal wallets from {args.mint[:12]}...\n")
    wallets = await extract_cabal_wallets(
        args.mint,
        min_profit_usd=args.min_profit,
        min_profit_mult=args.min_mult,
        max_wallets=args.max,
    )

    if not wallets:
        print("No wallets matched the filter criteria.")
        print("Try lowering --min-profit or --min-mult.")
        return

    print(f"Found {len(wallets)} cabal wallets:\n")
    print(f"{'#':>3}  {'Address':<46}  {'Profit':>12}  {'Mult':>6}  {'Cost':>10}  {'Tags'}")
    print("-" * 110)
    for i, w in enumerate(wallets, 1):
        tags = ", ".join(w.get("tags") or [])[:20]
        sm = " [SM]" if w.get("is_smart_money") else ""
        print(
            f"{i:3}  {w['address']:<46}  "
            f"${w['total_profit_usd']:>10,.0f}  "
            f"{w['profit_multiplier']:>5.1f}x  "
            f"${w['cost_usd']:>8,.0f}  "
            f"{tags}{sm}"
        )

    if args.no_add:
        print(f"\n--no-add specified. {len(wallets)} wallets NOT added to DB.")
        return

    db = SessionLocal()
    try:
        added = add_cabal_wallets_to_db(db, wallets, args.mint, args.symbol)
        print(f"\nAdded {added} new wallets to solana_known_wallets (role=cabal_trader).")
        if added < len(wallets):
            print(f"({len(wallets) - added} were already tracked.)")
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
