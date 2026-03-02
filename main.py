#!/usr/bin/env python3
"""
DCA Order Alert Scanner

Monitors Solana, Ethereum, and BSC for suspicious token activity:
- Volume spikes on low-cap tokens (<$50M market cap)
- Heavy buy pressure / DCA accumulation patterns
- Dead token revivals (sudden activity on dormant tokens)
- Stealth accumulation (buying without moving price)

Sends alerts via Telegram when potential pump setups are detected.

Usage:
    1. Copy .env.example to .env and fill in your Telegram bot token + chat ID
    2. pip install -r requirements.txt
    3. python main.py

    Or run a single scan:
    python main.py --once
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
    # Quiet down noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


async def run_once() -> None:
    """Run a single scan cycle and exit."""
    database.init_db()
    async with aiohttp.ClientSession() as session:
        stats = await scan_cycle(session)
    print(f"\nScan results: {stats}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DCA Order Alert Scanner - Detect suspicious crypto activity"
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single scan cycle and exit (useful for testing)"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging"
    )
    args = parser.parse_args()

    setup_logging(verbose=args.verbose)

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
