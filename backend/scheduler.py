"""
APScheduler job definitions — orchestrates pump-and-dump detection,
wallet tracking, and Solana cabal graph walk.

Memory management: the in-process scheduler runs several jobs on a 4GB
Render container. We use a module-level asyncio.Semaphore to cap
concurrent job execution and stagger first-runs across the first hour
to avoid boot-time spikes.
"""

import asyncio
import gc
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from backend.config import settings
from backend.scanners.volume_scanner import run_volume_scanner
from backend.scanners.profile_checker import run_profile_checker
from backend.scanners.wallet_analyzer import run_wallet_analyzer
from backend.scanners.exchange_flow import run_exchange_flow_monitor
from backend.scanners.social_scanner import run_social_scanner
from backend.scoring.scorer import run_score_recalculation
from backend.trackers.wallet_tracker import run_wallet_tracker
from backend.alerts.telegram_bot import fire_score_alert, send_daily_digest
from backend.database import SessionLocal
from backend.models.scan_log import ScanLog
from backend.models.flagged_token import FlaggedToken
from backend.models.watchlist import Watchlist
from backend.models.known_wallet import KnownWallet

# Solana cabal graph walk — follows where tracked cabal wallets
# send money so we auto-discover rotated wallets.
from backend.detection.solana_graph_walk import run_solana_graph_walk

logger = logging.getLogger(__name__)


# At most this many jobs may run concurrently. Even though APScheduler's
# AsyncIOScheduler runs everything in one event loop, parallel jobs each
# accumulate Python objects. 2GB container OOMs were the result of 5-6
# heavy jobs firing simultaneously; the semaphore enforces a cap.
MAX_CONCURRENT_JOBS = 2
_JOB_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

# Hard per-job timeout. A hung RPC / DB query / external API call is
# cancelled after this many seconds, releasing the semaphore for the
# next run. Without this, one stuck job takes the whole pipeline silent.
JOB_TIMEOUT_SECONDS = 8 * 60


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


# ─── Job bodies with logging + fire_score_alert integration ───────────

async def run_volume_scanner_job():
    started = datetime.utcnow()
    try:
        await run_volume_scanner()
        finished = datetime.utcnow()
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
    """Wallet tracker — stores activities; alerts fire via scorer only."""
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


# ─── Wrappers with semaphore + timeout + gc ───────────────────────────

def _wrap_throttled(fn, job_name: str | None = None):
    """Apply semaphore-bounded concurrency + hard timeout + gc to any job."""
    async def _wrapped():
        async with _JOB_SEMAPHORE:
            try:
                await asyncio.wait_for(fn(), timeout=JOB_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                logger.error(
                    f"{job_name or fn.__name__} timed out after "
                    f"{JOB_TIMEOUT_SECONDS}s — cancelled"
                )
                if job_name:
                    log_scan(
                        job_name, "error",
                        error_message=f"timeout after {JOB_TIMEOUT_SECONDS}s",
                        started_at=datetime.utcnow(), finished_at=datetime.utcnow(),
                    )
            except Exception as e:
                logger.error(f"{job_name or fn.__name__} failed: {e}")
            finally:
                gc.collect()
    _wrapped.__name__ = (job_name or fn.__name__) + "_throttled"
    return _wrapped


async def run_solana_graph_walk_job_inner():
    """Wraps run_solana_graph_walk with scan_log logging."""
    started = datetime.utcnow()
    try:
        result = await run_solana_graph_walk()
        log_scan(
            "solana_graph_walk", "success",
            details=str(result) if result else "ok",
            started_at=started, finished_at=datetime.utcnow(),
        )
    except Exception as e:
        logger.error(f"solana_graph_walk failed: {e}")
        log_scan(
            "solana_graph_walk", "error",
            error_message=str(e),
            started_at=started, finished_at=datetime.utcnow(),
        )


def setup_scheduler() -> AsyncIOScheduler:
    """
    Configure and return the APScheduler instance.
    """
    scheduler = AsyncIOScheduler(job_defaults={
        "max_instances": 1,
        "coalesce": True,
        "misfire_grace_time": 60 * 30,
    })

    now = datetime.utcnow()
    M = lambda mins: now + timedelta(minutes=mins)  # noqa: E731

    # (id, name, callable, trigger_kwargs, first_run_offset_min)
    jobs = [
        # Core BSC pump-detection pipeline
        ("volume_scanner",    "Volume Scanner",          _wrap_throttled(run_volume_scanner_job, "volume_scanner"),   {"minutes": settings.VOLUME_SCAN_INTERVAL}, 2),
        ("profile_checker",   "Profile Checker",         _wrap_throttled(run_profile_checker_job, "profile_checker"), {"minutes": settings.PROFILE_CHECK_INTERVAL}, 6),
        ("wallet_analyzer",   "Wallet Analyzer",         _wrap_throttled(run_wallet_analyzer_job, "wallet_analyzer"), {"minutes": settings.WALLET_ANALYZE_INTERVAL}, 15),
        ("exchange_flow",     "Exchange Flow Monitor",   _wrap_throttled(run_exchange_flow_job, "exchange_flow"),     {"minutes": settings.EXCHANGE_FLOW_INTERVAL}, 9),
        ("social_scanner",    "Social Scanner",          _wrap_throttled(run_social_scanner_job, "social_scanner"),   {"minutes": settings.SOCIAL_SCAN_INTERVAL}, 25),
        ("wallet_tracker",    "Wallet Tracker",          _wrap_throttled(run_wallet_tracker_job, "wallet_tracker"),   {"minutes": settings.WALLET_TRACK_INTERVAL}, 4),
        ("scorer",            "Score Recalculator",      _wrap_throttled(run_scorer_job, "scorer"),                   {"minutes": settings.SCORE_RECALC_INTERVAL}, 8),
        ("cleanup",           "Cleanup",                 _wrap_throttled(run_cleanup_job, "cleanup"),                 {"hours": 6}, 30),

        # Solana cabal graph walk — follows money from tracked cabal wallets
        ("solana_graph_walk", "Solana Graph Walk",       _wrap_throttled(run_solana_graph_walk_job_inner, "solana_graph_walk"), {"minutes": 15}, 12),
    ]

    for job_id, name, fn, trigger_kwargs, offset_min in jobs:
        scheduler.add_job(
            fn, "interval",
            id=job_id, name=name,
            next_run_time=M(offset_min),
            **trigger_kwargs,
        )

    # Daily digest — once per day at 20:00 UTC
    scheduler.add_job(
        send_daily_digest, "cron",
        hour=20, minute=0,
        id="daily_digest", name="Daily Digest",
    )

    return scheduler
