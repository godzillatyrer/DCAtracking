"""
APScheduler job definitions — orchestrates all scanner and tracker jobs.
Every job run is logged to the scan_logs table for observability.
"""

import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from backend.config import settings
from backend.scanners.volume_scanner import run_volume_scanner
from backend.scanners.profile_checker import run_profile_checker
from backend.scanners.wallet_analyzer import run_wallet_analyzer
from backend.scanners.exchange_flow import run_exchange_flow_monitor
from backend.scanners.social_scanner import run_social_scanner
from backend.scoring.scorer import run_score_recalculation
from backend.trackers.wallet_tracker import run_wallet_tracker
from backend.alerts.telegram_bot import fire_score_alert, fire_wallet_alert, send_daily_digest
from backend.database import SessionLocal
from backend.models.scan_log import ScanLog
from backend.models.flagged_token import FlaggedToken
from backend.models.watchlist import Watchlist
from backend.models.known_wallet import KnownWallet

logger = logging.getLogger(__name__)


def log_scan(job_name: str, status: str, tokens_checked: int = 0,
             tokens_flagged: int = 0, details: str = "", error_message: str = "",
             started_at: datetime = None, finished_at: datetime = None):
    """Write a scan log entry to the database."""
    db = SessionLocal()
    try:
        duration = None
        if started_at and finished_at:
            duration = int((finished_at - started_at).total_seconds())
        entry = ScanLog(
            job_name=job_name,
            status=status,
            tokens_checked=tokens_checked,
            tokens_flagged=tokens_flagged,
            details=details,
            error_message=error_message,
            started_at=started_at or datetime.utcnow(),
            finished_at=finished_at or datetime.utcnow(),
            duration_seconds=duration,
        )
        db.add(entry)
        db.commit()
    except Exception as e:
        logger.error(f"Failed to write scan log: {e}")
    finally:
        db.close()


async def run_volume_scanner_job():
    """Volume scanner with logging."""
    started = datetime.utcnow()
    try:
        await run_volume_scanner()
        finished = datetime.utcnow()
        # Count current state for the log
        db = SessionLocal()
        try:
            total_raw = db.query(FlaggedToken).filter(
                FlaggedToken.status == "raw", FlaggedToken.removed.is_(False)
            ).count()
            total_all = db.query(FlaggedToken).filter(
                FlaggedToken.removed.is_(False)
            ).count()
        finally:
            db.close()
        log_scan(
            "volume_scanner", "success",
            tokens_checked=total_all, tokens_flagged=total_raw,
            details=f"Scanned DEX Screener trending. {total_all} tokens tracked, {total_raw} raw flags.",
            started_at=started, finished_at=finished,
        )
    except Exception as e:
        logger.error(f"Volume scanner job failed: {e}")
        log_scan("volume_scanner", "error", error_message=str(e),
                 started_at=started, finished_at=datetime.utcnow())


async def run_profile_checker_job():
    """Profile checker with logging."""
    started = datetime.utcnow()
    try:
        await run_profile_checker()
        finished = datetime.utcnow()
        db = SessionLocal()
        try:
            raw_count = db.query(FlaggedToken).filter(FlaggedToken.status == "raw").count()
            candidate_count = db.query(FlaggedToken).filter(FlaggedToken.status == "candidate").count()
        finally:
            db.close()
        log_scan(
            "profile_checker", "success",
            tokens_checked=raw_count + candidate_count,
            details=f"Enriched token profiles via BscScan. {raw_count} raw remaining, {candidate_count} promoted to candidate.",
            started_at=started, finished_at=finished,
        )
    except Exception as e:
        logger.error(f"Profile checker job failed: {e}")
        log_scan("profile_checker", "error", error_message=str(e),
                 started_at=started, finished_at=datetime.utcnow())


async def run_wallet_analyzer_job():
    """Wallet analyzer with logging."""
    started = datetime.utcnow()
    try:
        await run_wallet_analyzer()
        finished = datetime.utcnow()
        db = SessionLocal()
        try:
            clusters = db.query(Watchlist).filter(Watchlist.cluster_detected.is_(True)).count()
            watchlist_count = db.query(Watchlist).count()
        finally:
            db.close()
        log_scan(
            "wallet_analyzer", "success",
            tokens_checked=watchlist_count,
            tokens_flagged=clusters,
            details=f"Analyzed holder clusters. {watchlist_count} tokens on watchlist, {clusters} with clusters detected.",
            started_at=started, finished_at=finished,
        )
    except Exception as e:
        logger.error(f"Wallet analyzer job failed: {e}")
        log_scan("wallet_analyzer", "error", error_message=str(e),
                 started_at=started, finished_at=datetime.utcnow())


async def run_exchange_flow_job():
    """Exchange flow monitor with logging."""
    started = datetime.utcnow()
    try:
        await run_exchange_flow_monitor()
        finished = datetime.utcnow()
        db = SessionLocal()
        try:
            with_deposits = db.query(Watchlist).filter(
                Watchlist.exchange_deposits_detected.is_(True)
            ).count()
            watchlist_count = db.query(FlaggedToken).filter(
                FlaggedToken.status == "watchlist"
            ).count()
        finally:
            db.close()
        log_scan(
            "exchange_flow", "success",
            tokens_checked=watchlist_count,
            tokens_flagged=with_deposits,
            details=f"Monitored exchange flows for {watchlist_count} tokens. {with_deposits} with insider deposits.",
            started_at=started, finished_at=finished,
        )
    except Exception as e:
        logger.error(f"Exchange flow monitor job failed: {e}")
        log_scan("exchange_flow", "error", error_message=str(e),
                 started_at=started, finished_at=datetime.utcnow())


async def run_social_scanner_job():
    """Social scanner with logging."""
    started = datetime.utcnow()
    try:
        await run_social_scanner()
        finished = datetime.utcnow()
        db = SessionLocal()
        try:
            social_detected = db.query(Watchlist).filter(
                Watchlist.social_signal_detected.is_(True)
            ).count()
        finally:
            db.close()
        log_scan(
            "social_scanner", "success",
            tokens_flagged=social_detected,
            details=f"Scanned social signals. {social_detected} tokens with social activity detected.",
            started_at=started, finished_at=finished,
        )
    except Exception as e:
        logger.error(f"Social scanner job failed: {e}")
        log_scan("social_scanner", "error", error_message=str(e),
                 started_at=started, finished_at=datetime.utcnow())


async def run_wallet_tracker_job():
    """Wallet tracker with logging and alert triggering."""
    started = datetime.utcnow()
    try:
        notable = await run_wallet_tracker()
        finished = datetime.utcnow()
        db = SessionLocal()
        try:
            active_wallets = db.query(KnownWallet).filter(
                KnownWallet.is_active.is_(True)
            ).count()
        finally:
            db.close()

        notable_summary = ""
        if notable:
            notable_summary = " | ".join(
                f"{n.get('wallet_label','?')}: {n['activity']} {n.get('token_symbol','')}"
                for n in notable[:5]
            )
        log_scan(
            "wallet_tracker", "success",
            tokens_checked=active_wallets,
            tokens_flagged=len(notable),
            details=f"Tracked {active_wallets} known wallets. {len(notable)} notable activities. {notable_summary}",
            started_at=started, finished_at=finished,
        )

        # Fire alerts for high-priority activities
        for item in notable:
            if item.get("is_new_token") or item.get("activity") == "exchange_deposit":
                await fire_wallet_alert(item)
    except Exception as e:
        logger.error(f"Wallet tracker job failed: {e}")
        log_scan("wallet_tracker", "error", error_message=str(e),
                 started_at=started, finished_at=datetime.utcnow())


async def run_scorer_job():
    """Score recalculation with logging and alert triggering."""
    started = datetime.utcnow()
    try:
        alert_candidates = await run_score_recalculation()
        finished = datetime.utcnow()
        db = SessionLocal()
        try:
            total_scored = db.query(FlaggedToken).filter(
                FlaggedToken.status.in_(["raw", "candidate", "watchlist"]),
                FlaggedToken.removed.is_(False),
            ).count()
            high_score = db.query(Watchlist).filter(Watchlist.current_score >= 70).count()
            med_score = db.query(Watchlist).filter(
                Watchlist.current_score >= 50, Watchlist.current_score < 70
            ).count()
        finally:
            db.close()

        alert_names = ", ".join(c["symbol"] for c in alert_candidates) if alert_candidates else "none"
        log_scan(
            "scorer", "success",
            tokens_checked=total_scored,
            tokens_flagged=len(alert_candidates),
            details=f"Scored {total_scored} tokens. {high_score} HIGH (>=70), {med_score} MEDIUM (50-69). New alerts: {alert_names}",
            started_at=started, finished_at=finished,
        )

        # Fire alerts
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
        log_scan("scorer", "error", error_message=str(e),
                 started_at=started, finished_at=datetime.utcnow())


async def run_cleanup_job():
    """Expire old flags and log."""
    from backend.scanners.volume_scanner import expire_old_flags

    started = datetime.utcnow()
    try:
        db = SessionLocal()
        try:
            expire_old_flags(db)
        finally:
            db.close()
        log_scan(
            "cleanup", "success",
            details="Expired old flagged tokens (>7 days unseen).",
            started_at=started, finished_at=datetime.utcnow(),
        )
    except Exception as e:
        logger.error(f"Cleanup job failed: {e}")
        log_scan("cleanup", "error", error_message=str(e),
                 started_at=started, finished_at=datetime.utcnow())


def setup_scheduler() -> AsyncIOScheduler:
    """Configure and return the APScheduler instance."""
    scheduler = AsyncIOScheduler()

    # Step 1: Volume scanner — every 15 minutes
    scheduler.add_job(
        run_volume_scanner_job, "interval",
        minutes=settings.VOLUME_SCAN_INTERVAL,
        id="volume_scanner", name="Volume Scanner",
    )

    # Step 2: Profile checker — every 1 hour
    scheduler.add_job(
        run_profile_checker_job, "interval",
        minutes=settings.PROFILE_CHECK_INTERVAL,
        id="profile_checker", name="Profile Checker",
    )

    # Step 3: Wallet analyzer — every 4 hours
    scheduler.add_job(
        run_wallet_analyzer_job, "interval",
        minutes=settings.WALLET_ANALYZE_INTERVAL,
        id="wallet_analyzer", name="Wallet Analyzer",
    )

    # Step 4: Exchange flow monitor — every 30 minutes
    scheduler.add_job(
        run_exchange_flow_job, "interval",
        minutes=settings.EXCHANGE_FLOW_INTERVAL,
        id="exchange_flow", name="Exchange Flow Monitor",
    )

    # Step 5: Social scanner — every 2 hours
    scheduler.add_job(
        run_social_scanner_job, "interval",
        minutes=settings.SOCIAL_SCAN_INTERVAL,
        id="social_scanner", name="Social Scanner",
    )

    # Known wallet tracker — every 30 minutes
    scheduler.add_job(
        run_wallet_tracker_job, "interval",
        minutes=settings.WALLET_TRACK_INTERVAL,
        id="wallet_tracker", name="Wallet Tracker",
    )

    # Score recalculation — every 30 minutes
    scheduler.add_job(
        run_scorer_job, "interval",
        minutes=settings.SCORE_RECALC_INTERVAL,
        id="scorer", name="Score Recalculator",
    )

    # Cleanup — every 24 hours
    scheduler.add_job(
        run_cleanup_job, "interval",
        hours=24,
        id="cleanup", name="Cleanup",
    )

    # Daily digest — once per day at 20:00 UTC
    scheduler.add_job(
        send_daily_digest, "cron",
        hour=20, minute=0,
        id="daily_digest", name="Daily Digest",
    )

    return scheduler
