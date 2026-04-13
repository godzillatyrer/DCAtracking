"""
APScheduler job definitions — orchestrates all scanner and tracker jobs.
"""

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from backend.config import settings
from backend.scanners.volume_scanner import run_volume_scanner
from backend.scanners.profile_checker import run_profile_checker
from backend.scanners.wallet_analyzer import run_wallet_analyzer
from backend.scanners.exchange_flow import run_exchange_flow_monitor
from backend.scanners.social_scanner import run_social_scanner
from backend.scoring.scorer import run_score_recalculation
from backend.trackers.wallet_tracker import run_wallet_tracker
from backend.alerts.telegram_bot import fire_score_alert, fire_wallet_alert
from backend.database import SessionLocal

logger = logging.getLogger(__name__)


async def run_volume_scanner_job():
    """Wrapper for volume scanner with error handling."""
    try:
        await run_volume_scanner()
    except Exception as e:
        logger.error(f"Volume scanner job failed: {e}")


async def run_profile_checker_job():
    """Wrapper for profile checker with error handling."""
    try:
        await run_profile_checker()
    except Exception as e:
        logger.error(f"Profile checker job failed: {e}")


async def run_wallet_analyzer_job():
    """Wrapper for wallet analyzer with error handling."""
    try:
        await run_wallet_analyzer()
    except Exception as e:
        logger.error(f"Wallet analyzer job failed: {e}")


async def run_exchange_flow_job():
    """Wrapper for exchange flow monitor with error handling."""
    try:
        await run_exchange_flow_monitor()
    except Exception as e:
        logger.error(f"Exchange flow monitor job failed: {e}")


async def run_social_scanner_job():
    """Wrapper for social scanner with error handling."""
    try:
        await run_social_scanner()
    except Exception as e:
        logger.error(f"Social scanner job failed: {e}")


async def run_wallet_tracker_job():
    """Wrapper for wallet tracker with alert triggering."""
    try:
        notable = await run_wallet_tracker()
        # Fire alerts for high-priority activities
        for item in notable:
            if item.get("is_new_token") or item.get("activity") == "exchange_deposit":
                await fire_wallet_alert(item)
    except Exception as e:
        logger.error(f"Wallet tracker job failed: {e}")


async def run_scorer_job():
    """Wrapper for score recalculation with alert triggering."""
    try:
        alert_candidates = await run_score_recalculation()
        # Fire alerts for tokens crossing the threshold
        if alert_candidates:
            db = SessionLocal()
            try:
                for candidate in alert_candidates:
                    await fire_score_alert(
                        candidate["contract_address"],
                        candidate["score"],
                        candidate["breakdown"],
                        db=db,
                    )
            finally:
                db.close()
    except Exception as e:
        logger.error(f"Scorer job failed: {e}")


async def run_cleanup_job():
    """Expire old flagged tokens and clean up stale data."""
    from backend.scanners.volume_scanner import expire_old_flags
    from backend.database import SessionLocal

    logger.info("Running cleanup job...")
    db = SessionLocal()
    try:
        expire_old_flags(db)
        logger.info("Cleanup complete.")
    except Exception as e:
        logger.error(f"Cleanup job failed: {e}")
    finally:
        db.close()


def setup_scheduler() -> AsyncIOScheduler:
    """Configure and return the APScheduler instance."""
    scheduler = AsyncIOScheduler()

    # Step 1: Volume scanner — every 15 minutes
    scheduler.add_job(
        run_volume_scanner_job,
        "interval",
        minutes=settings.VOLUME_SCAN_INTERVAL,
        id="volume_scanner",
        name="Volume Scanner",
    )

    # Step 2: Profile checker — every 1 hour
    scheduler.add_job(
        run_profile_checker_job,
        "interval",
        minutes=settings.PROFILE_CHECK_INTERVAL,
        id="profile_checker",
        name="Profile Checker",
    )

    # Step 3: Wallet analyzer — every 4 hours
    scheduler.add_job(
        run_wallet_analyzer_job,
        "interval",
        minutes=settings.WALLET_ANALYZE_INTERVAL,
        id="wallet_analyzer",
        name="Wallet Analyzer",
    )

    # Step 4: Exchange flow monitor — every 30 minutes
    scheduler.add_job(
        run_exchange_flow_job,
        "interval",
        minutes=settings.EXCHANGE_FLOW_INTERVAL,
        id="exchange_flow",
        name="Exchange Flow Monitor",
    )

    # Step 5: Social scanner — every 2 hours
    scheduler.add_job(
        run_social_scanner_job,
        "interval",
        minutes=settings.SOCIAL_SCAN_INTERVAL,
        id="social_scanner",
        name="Social Scanner",
    )

    # Known wallet tracker — every 30 minutes
    scheduler.add_job(
        run_wallet_tracker_job,
        "interval",
        minutes=settings.WALLET_TRACK_INTERVAL,
        id="wallet_tracker",
        name="Wallet Tracker",
    )

    # Score recalculation — every 30 minutes
    scheduler.add_job(
        run_scorer_job,
        "interval",
        minutes=settings.SCORE_RECALC_INTERVAL,
        id="scorer",
        name="Score Recalculator",
    )

    # Cleanup: expire old flags — every 24 hours
    scheduler.add_job(
        run_cleanup_job,
        "interval",
        hours=24,
        id="cleanup",
        name="Cleanup",
    )

    return scheduler
