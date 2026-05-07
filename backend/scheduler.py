"""
APScheduler — Solana CEX-funded accumulation tracking jobs.

Runs in-process via the FastAPI lifespan. Concurrency is capped so the
256mb Render database + starter plan worker don't OOM when the graph
walk pulls many tx bodies at once.

Pipeline:
  1. cex_outflow_harvester  — discover wallets funded from CEX hot
                              wallets (Binance/OKX/Bybit/Bitget/etc.),
                              insert as role='cex_funded'.
  2. wallet_activity_tracker — already-existing job. Picks up the new
                              cex_funded wallets, records their SPL
                              buys/sells in solana_wallet_activity.
  3. accumulation_alerter   — aggregates net holdings across the
                              cex_funded population per mint, computes
                              supply %, fires Telegram alert when N+
                              CEX-funded wallets cross the supply
                              threshold on a coin in the target mcap
                              band.
  4. solana_graph_walk      — finds sibling wallets via SOL hops.
  5. dca_order_watcher      — Jupiter DCA on low-cap tokens.
"""

import asyncio
import gc
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from backend.database import SessionLocal
from backend.alerts.telegram_commands import run_telegram_command_poller
from backend.anomaly.accumulation_alerter import run_accumulation_alerter
from backend.anomaly.cex_outflow_harvester import run_cex_outflow_harvester
from backend.anomaly.dca_order_watcher import run_dca_order_watcher
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


def _runner(name, fn):
    """Build a `started/log/error` wrapper around an async job function."""
    async def _job():
        started = datetime.utcnow()
        try:
            result = await fn()
            log_scan(name, "success",
                     details=str(result) if result else "ok",
                     started_at=started, finished_at=datetime.utcnow())
        except Exception as e:
            logger.error(f"{name} failed: {e}")
            log_scan(name, "error", error_message=str(e),
                     started_at=started, finished_at=datetime.utcnow())
    _job.__name__ = f"{name}_runner"
    return _job


async def run_wallet_activity_tracker_job():
    """Activity tracker is special — it also kicks the convergence
    dispatcher every cycle so cabal-aping alerts fire promptly."""
    started = datetime.utcnow()
    try:
        result = await run_wallet_activity_tracker()
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


async def run_telegram_command_poller_job():
    """Polls Telegram /getUpdates for inbound bot commands. Runs much
    more often than the watchers (10s) so user commands feel snappy."""
    try:
        await run_telegram_command_poller()
    except Exception as e:
        logger.error(f"telegram_command_poller failed: {e}")


def setup_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(job_defaults={
        "max_instances": 1,
        "coalesce": True,
        "misfire_grace_time": 60 * 30,
    })

    now = datetime.utcnow()

    # ── CEX-funded wallet pipeline (the new core) ──────────────────
    scheduler.add_job(
        _wrap_throttled(_runner("cex_outflow_harvester", run_cex_outflow_harvester),
                        "cex_outflow_harvester"),
        "interval",
        id="cex_outflow_harvester", name="CEX Outflow Harvester",
        minutes=10,
        next_run_time=now + timedelta(seconds=20),
    )
    scheduler.add_job(
        _wrap_throttled(_runner("accumulation_alerter", run_accumulation_alerter),
                        "accumulation_alerter"),
        "interval",
        id="accumulation_alerter", name="CEX Accumulation Alerter",
        minutes=20,
        next_run_time=now + timedelta(minutes=3),
    )

    # ── Existing Solana cabal pipeline ─────────────────────────────
    scheduler.add_job(
        _wrap_throttled(_runner("solana_graph_walk", run_solana_graph_walk),
                        "solana_graph_walk"),
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
        _wrap_throttled(_runner("wallet_stats_aggregator", run_wallet_stats_aggregator),
                        "wallet_stats_aggregator"),
        "interval",
        id="wallet_stats_aggregator", name="Wallet Stats Aggregator",
        minutes=10,
        next_run_time=now + timedelta(minutes=1),
    )
    scheduler.add_job(
        _wrap_throttled(_runner("alert_outcome_tracker", run_alert_outcome_tracker),
                        "alert_outcome_tracker"),
        "interval",
        id="alert_outcome_tracker", name="Alert Outcome Tracker",
        minutes=10,
        next_run_time=now + timedelta(minutes=2),
    )
    scheduler.add_job(
        _wrap_throttled(_runner("behavioral_clusterer", run_behavioral_clusterer),
                        "behavioral_clusterer"),
        "cron",
        id="behavioral_clusterer", name="Behavioral Clusterer",
        hour=3, minute=15,   # 03:15 UTC nightly
    )
    scheduler.add_job(
        _wrap_throttled(_runner("dca_order_watcher", run_dca_order_watcher),
                        "dca_order_watcher"),
        "interval",
        id="dca_order_watcher", name="Jupiter DCA Order Watcher",
        minutes=3,
        next_run_time=now + timedelta(seconds=90),
    )
    # Telegram command poller — short interval, NOT wrapped in
    # _wrap_throttled because we want it lightweight + always on. The
    # poller has its own light error handling.
    scheduler.add_job(
        run_telegram_command_poller_job,
        "interval",
        id="telegram_command_poller", name="Telegram Command Poller",
        seconds=10,
        max_instances=1,
        coalesce=True,
        next_run_time=now + timedelta(seconds=15),
    )

    return scheduler
