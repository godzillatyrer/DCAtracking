"""Main scanner that orchestrates token discovery, analysis, and alerting."""

import asyncio
import logging
import time

import aiohttp

import config
import database
import dexscreener
from analyzer import analyze_token, compute_crime_score
from alerts import send_alert

logger = logging.getLogger(__name__)

# Minimum crime score to trigger an alert
MIN_ALERT_SCORE = 25.0


async def scan_cycle(session: aiohttp.ClientSession) -> dict:
    """Run one full scan cycle. Returns stats about the scan."""
    stats = {
        "tokens_scanned": 0,
        "signals_found": 0,
        "alerts_sent": 0,
        "errors": 0,
    }

    logger.info("Starting scan cycle...")
    start = time.time()

    # Step 1: Discover tokens
    try:
        tokens = await dexscreener.discover_tokens(session)
    except Exception as e:
        logger.error("Token discovery failed: %s", e)
        stats["errors"] += 1
        return stats

    stats["tokens_scanned"] = len(tokens)
    logger.info("Scanning %d tokens...", len(tokens))

    # Step 2: Analyze each token
    for token_data in tokens:
        try:
            signals = analyze_token(token_data)
            if not signals:
                continue

            stats["signals_found"] += len(signals)
            crime_score = compute_crime_score(signals)

            # Only alert if crime score meets threshold
            if crime_score < MIN_ALERT_SCORE:
                continue

            # Send alert for the highest-severity signal
            # (to avoid spamming multiple alerts for same token)
            best_signal = max(signals, key=lambda s: s.score)
            sent = await send_alert(best_signal, crime_score, token_data)
            if sent:
                stats["alerts_sent"] += 1

        except Exception as e:
            logger.error(
                "Error analyzing %s: %s",
                token_data.get("token_symbol", "???"), e,
            )
            stats["errors"] += 1

    elapsed = time.time() - start
    logger.info(
        "Scan complete in %.1fs: %d tokens, %d signals, %d alerts sent",
        elapsed, stats["tokens_scanned"], stats["signals_found"],
        stats["alerts_sent"],
    )
    return stats


async def run_scanner() -> None:
    """Run the scanner in a continuous loop."""
    database.init_db()
    logger.info("DCA Order Alert Scanner started")
    logger.info(
        "Config: chains=%s, max_mcap=%s, volume_spike=%sx, interval=%ds",
        config.CHAINS, config.MAX_MARKET_CAP,
        config.VOLUME_SPIKE_MULTIPLIER, config.SCAN_INTERVAL_SECONDS,
    )

    if not config.TELEGRAM_BOT_TOKEN:
        logger.warning(
            "TELEGRAM_BOT_TOKEN not set - alerts will be logged to console only"
        )

    scan_count = 0
    async with aiohttp.ClientSession() as session:
        while True:
            scan_count += 1
            logger.info("=== Scan #%d ===", scan_count)

            try:
                stats = await scan_cycle(session)

                # Periodic cleanup (every 50 scans)
                if scan_count % 50 == 0:
                    database.cleanup_old_data(days=7)
                    logger.info("Database cleanup complete")

            except Exception as e:
                logger.error("Scan cycle failed: %s", e)

            logger.info(
                "Next scan in %d seconds...", config.SCAN_INTERVAL_SECONDS
            )
            await asyncio.sleep(config.SCAN_INTERVAL_SECONDS)
