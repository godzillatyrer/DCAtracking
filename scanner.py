"""Main scan loop: monitors Jupiter DCA orders and detects suspicious patterns."""

import asyncio
import logging
import time

import aiohttp

import config
import database
import jupiter_dca
import token_data as td
from analyzer import analyze_dca_order, compute_crime_score
from alerts import send_alert

logger = logging.getLogger(__name__)

MIN_ALERT_SCORE = 20.0


async def scan_cycle(session: aiohttp.ClientSession) -> dict:
    """Run one full scan cycle."""
    stats = {
        "dca_orders_found": 0,
        "tokens_analyzed": 0,
        "signals_found": 0,
        "alerts_sent": 0,
        "errors": 0,
    }

    start = time.time()

    # Step 1: Scan for new Jupiter DCA orders
    try:
        new_orders = await jupiter_dca.scan_new_dca_orders(session)
        stats["dca_orders_found"] = len(new_orders)
    except Exception as e:
        logger.error("DCA scan failed: %s", e)
        stats["errors"] += 1
        return stats

    if not new_orders:
        logger.info("No new DCA orders found this cycle")
        return stats

    # Step 2: Calculate USD value for each order
    input_mints = list(set(o["input_mint"] for o in new_orders))
    input_prices = await td.get_token_prices_batch(session, input_mints)

    for order in new_orders:
        input_mint = order["input_mint"]
        raw_amount = order["in_amount_raw"]

        human_amount = td.convert_raw_amount(raw_amount, input_mint)
        price_info = input_prices.get(input_mint, {})
        price = price_info.get("price_usd", 0)

        if price > 0:
            order["in_amount_usd"] = human_amount * price
        elif input_mint in (
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
        ):
            order["in_amount_usd"] = human_amount
        else:
            order["in_amount_usd"] = 0

        # Fill defaults and save
        order.setdefault("token_symbol", "")
        order.setdefault("token_name", "")
        order.setdefault("token_mcap", 0)
        order.setdefault("token_price", 0)
        database.save_dca_order(order)

    # Step 3: Get token data for all target tokens
    output_mints = list(set(o["output_mint"] for o in new_orders))
    logger.info("Analyzing %d unique target tokens...", len(output_mints))

    semaphore = asyncio.Semaphore(3)

    async def fetch_with_limit(mint: str):
        async with semaphore:
            data = await td.get_token_full_data(session, mint)
            await asyncio.sleep(0.5)  # respect GeckoTerminal rate limits
            return mint, data

    tasks = [fetch_with_limit(mint) for mint in output_mints]
    token_results = await asyncio.gather(*tasks, return_exceptions=True)

    token_info_map = {}
    for result in token_results:
        if isinstance(result, Exception):
            logger.debug("Error fetching token data: %s", result)
            continue
        mint, data = result
        token_info_map[mint] = data

    # Step 4: Analyze each target token
    for output_mint in output_mints:
        try:
            token_info = token_info_map.get(output_mint)

            # Skip tokens above max market cap (if we know the mcap)
            if token_info and token_info.get("market_cap", 0) > config.MAX_MARKET_CAP:
                if token_info["market_cap"] > 0:
                    logger.debug(
                        "Skipping %s - mcap %s above threshold",
                        output_mint[:16], token_info["market_cap"],
                    )
                    continue

            # Save volume snapshot for historical tracking
            if token_info:
                database.save_volume_snapshot({
                    "token_mint": output_mint,
                    "chain": "solana",
                    "volume_24h": token_info.get("volume_24h", 0),
                    "price_usd": token_info.get("price_usd", 0),
                    "market_cap": token_info.get("market_cap", 0),
                    "liquidity_usd": token_info.get("liquidity_usd", 0),
                    "timestamp": time.time(),
                })

            stats["tokens_analyzed"] += 1

            # Analyze each order targeting this token
            all_signals = []
            token_orders = [o for o in new_orders if o["output_mint"] == output_mint]

            for order in token_orders:
                signals = analyze_dca_order(order, token_info)
                all_signals.extend(signals)

            if not all_signals:
                continue

            # Deduplicate: keep highest-scoring signal per type
            best_per_type = {}
            for sig in all_signals:
                if (sig.signal_type not in best_per_type
                        or sig.score > best_per_type[sig.signal_type].score):
                    best_per_type[sig.signal_type] = sig

            unique_signals = list(best_per_type.values())
            stats["signals_found"] += len(unique_signals)

            crime_score = compute_crime_score(unique_signals)

            if crime_score < MIN_ALERT_SCORE:
                continue

            # Send alert for the highest-severity signal
            best_signal = max(unique_signals, key=lambda s: s.score)
            sent = await send_alert(best_signal, crime_score, token_info)
            if sent:
                stats["alerts_sent"] += 1

        except Exception as e:
            logger.error("Error analyzing token %s: %s", output_mint[:16], e)
            stats["errors"] += 1

    elapsed = time.time() - start
    logger.info(
        "Scan complete in %.1fs: %d DCA orders, %d tokens, %d signals, %d alerts",
        elapsed, stats["dca_orders_found"], stats["tokens_analyzed"],
        stats["signals_found"], stats["alerts_sent"],
    )
    return stats


async def run_scanner() -> None:
    """Run the scanner in a continuous loop."""
    database.init_db()
    logger.info("DCA Order Tracker started")
    logger.info("Monitoring: Jupiter DCA program on Solana")
    logger.info(
        "Config: max_mcap=$%s, min_dca=$%s, interval=%ds",
        config.MAX_MARKET_CAP, config.MIN_DCA_VALUE_USD,
        config.SCAN_INTERVAL_SECONDS,
    )

    if not config.HELIUS_API_KEY:
        logger.error("HELIUS_API_KEY not set - cannot monitor Solana")
        return

    if not config.TELEGRAM_BOT_TOKEN:
        logger.warning(
            "TELEGRAM_BOT_TOKEN not set - alerts will log to console only"
        )

    scan_count = 0
    async with aiohttp.ClientSession() as session:
        while True:
            scan_count += 1
            logger.info("=== Scan #%d ===", scan_count)

            try:
                await scan_cycle(session)

                if scan_count % 50 == 0:
                    database.cleanup_old_data(days=14)
                    logger.info("Database cleanup complete")

            except Exception as e:
                logger.error("Scan cycle failed: %s", e)

            logger.info(
                "Next scan in %ds...", config.SCAN_INTERVAL_SECONDS
            )
            await asyncio.sleep(config.SCAN_INTERVAL_SECONDS)
