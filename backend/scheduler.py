"""
APScheduler — Solana cabal tracking jobs.

Runs in-process via the FastAPI lifespan. Concurrency is capped so the
256mb Render database + starter plan worker don't OOM when the graph
walk pulls many tx bodies at once.
"""

import asyncio
import gc
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from backend.database import SessionLocal
from backend.anomaly.hyperliquid_watcher import run_hyperliquid_watcher
from backend.detection.alert_dispatcher import run_alert_dispatch
from backend.detection.alert_outcome_tracker import run_alert_outcome_tracker
from backend.detection.behavioral_clusterer import run_behavioral_clusterer
from backend.detection.solana_graph_walk import run_solana_graph_walk
from backend.detection.wallet_activity_tracker import run_wallet_activity_tracker
from backend.detection.wallet_stats_aggregator import run_wallet_stats_aggregator
from backend.models.scan_log import ScanLog

logger = logging.getLogger(__name__)


MAX_CONCURRENT_JOBS = 2
_JOB_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
JOB_TIMEOUT_SECONDS = 8 * 60


def log_scan(job_name: str, status: str, details: str = "",
             error_message: str = "",
             started_at: datetime = None, finished_at: datetime = None):
    db = SessionLocal()
    try:
        duration = None
        if started_at and finished_at:
            duration = int((finished_at - started_at).total_seconds())
        entry = ScanLog(
            job_name=job_name,
            status=status,
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


def _wrap_throttled(fn, job_name: str):
    """Semaphore + hard timeout + gc around any job body."""
    async def _wrapped():
        async with _JOB_SEMAPHORE:
            try:
                await asyncio.wait_for(fn(), timeout=JOB_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                logger.error(f"{job_name} timed out after {JOB_TIMEOUT_SECONDS}s")
                log_scan(job_name, "error",
                         error_message=f"timeout after {JOB_TIMEOUT_SECONDS}s",
                         started_at=datetime.utcnow(), finished_at=datetime.utcnow())
            except Exception as e:
                logger.error(f"{job_name} failed: {e}")
            finally:
                gc.collect()
    _wrapped.__name__ = f"{job_name}_throttled"
    return _wrapped


async def run_solana_graph_walk_job():
    started = datetime.utcnow()
    try:
        result = await run_solana_graph_walk()
        log_scan("solana_graph_walk", "success",
                 details=str(result) if result else "ok",
                 started_at=started, finished_at=datetime.utcnow())
    except Exception as e:
        logger.error(f"solana_graph_walk failed: {e}")
        log_scan("solana_graph_walk", "error", error_message=str(e),
                 started_at=started, finished_at=datetime.utcnow())


async def run_wallet_activity_tracker_job():
    started = datetime.utcnow()
    try:
        result = await run_wallet_activity_tracker()
        # Dispatch alerts on fresh activity right away. Dedup handles
        # re-firing; this is safe to call every cycle.
        try:
            alerts = await run_alert_dispatch()
            if alerts:
                result = {**(result or {}), "alerts": alerts}
        except Exception as e:
            logger.error(f"alert_dispatch during tracker run failed: {e}")
        log_scan("wallet_activity_tracker", "success",
                 details=str(result) if result else "ok",
                 started_at=started, finished_at=datetime.utcnow())
    except Exception as e:
        logger.error(f"wallet_activity_tracker failed: {e}")
        log_scan("wallet_activity_tracker", "error", error_message=str(e),
                 started_at=started, finished_at=datetime.utcnow())


async def run_wallet_stats_aggregator_job():
    started = datetime.utcnow()
    try:
        result = await run_wallet_stats_aggregator()
        log_scan("wallet_stats_aggregator", "success",
                 details=str(result) if result else "ok",
                 started_at=started, finished_at=datetime.utcnow())
    except Exception as e:
        logger.error(f"wallet_stats_aggregator failed: {e}")
        log_scan("wallet_stats_aggregator", "error", error_message=str(e),
                 started_at=started, finished_at=datetime.utcnow())


async def run_alert_outcome_tracker_job():
    started = datetime.utcnow()
    try:
        result = await run_alert_outcome_tracker()
        log_scan("alert_outcome_tracker", "success",
                 details=str(result) if result else "ok",
                 started_at=started, finished_at=datetime.utcnow())
    except Exception as e:
        logger.error(f"alert_outcome_tracker failed: {e}")
        log_scan("alert_outcome_tracker", "error", error_message=str(e),
                 started_at=started, finished_at=datetime.utcnow())


async def run_behavioral_clusterer_job():
    started = datetime.utcnow()
    try:
        result = await run_behavioral_clusterer()
        log_scan("behavioral_clusterer", "success",
                 details=str(result) if result else "ok",
                 started_at=started, finished_at=datetime.utcnow())
    except Exception as e:
        logger.error(f"behavioral_clusterer failed: {e}")
        log_scan("behavioral_clusterer", "error", error_message=str(e),
                 started_at=started, finished_at=datetime.utcnow())


async def run_hyperliquid_watcher_job():
    started = datetime.utcnow()
    try:
        result = await run_hyperliquid_watcher()
        log_scan("hyperliquid_watcher", "success",
                 details=str(result) if result else "ok",
                 started_at=started, finished_at=datetime.utcnow())
    except Exception as e:
        logger.error(f"hyperliquid_watcher failed: {e}")
        log_scan("hyperliquid_watcher", "error", error_message=str(e),
                 started_at=started, finished_at=datetime.utcnow())


def setup_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(job_defaults={
        "max_instances": 1,
        "coalesce": True,
        "misfire_grace_time": 60 * 30,
    })

    now = datetime.utcnow()
    scheduler.add_job(
        _wrap_throttled(run_solana_graph_walk_job, "solana_graph_walk"),
        "interval",
        id="solana_graph_walk", name="Solana Graph Walk",
        minutes=15,
        next_run_time=now + timedelta(minutes=2),
    )
    scheduler.add_job(
        _wrap_throttled(run_wallet_activity_tracker_job, "wallet_activity_tracker"),
        "interval",
        id="wallet_activity_tracker", name="Wallet Activity Tracker",
        minutes=5,
        next_run_time=now + timedelta(seconds=30),
    )
    scheduler.add_job(
        _wrap_throttled(run_wallet_stats_aggregator_job, "wallet_stats_aggregator"),
        "interval",
        id="wallet_stats_aggregator", name="Wallet Stats Aggregator",
        minutes=10,
        next_run_time=now + timedelta(minutes=1),
    )
    scheduler.add_job(
        _wrap_throttled(run_alert_outcome_tracker_job, "alert_outcome_tracker"),
        "interval",
        id="alert_outcome_tracker", name="Alert Outcome Tracker",
        minutes=10,
        next_run_time=now + timedelta(minutes=2),
    )
    scheduler.add_job(
        _wrap_throttled(run_behavioral_clusterer_job, "behavioral_clusterer"),
        "cron",
        id="behavioral_clusterer", name="Behavioral Clusterer",
        hour=3, minute=15,   # 03:15 UTC nightly
    )
    scheduler.add_job(
        _wrap_throttled(run_hyperliquid_watcher_job, "hyperliquid_watcher"),
        "interval",
        id="hyperliquid_watcher", name="Hyperliquid Whale Watcher",
        minutes=1,
        next_run_time=now + timedelta(seconds=20),
    )

    return scheduler
