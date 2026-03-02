#!/usr/bin/env python3
"""
DCA Order Tracker

Monitors Jupiter DCA program on Solana for large DCA orders ($150K+)
on sub-$50M mcap tokens. Alerts when serious capital is being deployed
to protect a level or accumulate before a move.

Signals:
- Mega DCA: single order >= $150K
- Coordinated accumulation: multiple wallets totalling >= $150K in 24h

Sends alerts via Telegram for leveraged long entries.

Usage:
    1. Copy .env.example to .env and fill in your API keys
    2. pip install -r requirements.txt
    3. python main.py

    Single scan:  python main.py --once
    Debug mode:   python main.py -v
"""

import argparse
import asyncio
import logging
import sys

import aiohttp

import config
import database
from scanner import scan_cycle, run_scanner


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def run_once() -> None:
    """Run a single scan cycle and exit."""
    database.init_db()
    async with aiohttp.ClientSession() as session:
        stats = await scan_cycle(session)
    print(f"\nScan results: {stats}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DCA Order Tracker - Detect suspicious DCA activity on Solana"
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single scan cycle and exit (useful for testing)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    setup_logging(verbose=args.verbose)

    if not config.HELIUS_API_KEY:
        print("ERROR: HELIUS_API_KEY not set in .env file")
        print("Get a free API key at https://helius.dev (100K credits/day)")
        sys.exit(1)

    try:
        if args.once:
            asyncio.run(run_once())
        else:
            asyncio.run(run_scanner())
    except KeyboardInterrupt:
        print("\nScanner stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
