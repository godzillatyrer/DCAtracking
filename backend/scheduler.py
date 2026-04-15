"""
APScheduler job definitions — orchestrates all scanner and tracker jobs.
Every job run is logged to the scan_logs table for observability.

Memory management: the in-process scheduler runs 18+ jobs on a single
2GB Render container. Without limits, staggered first-runs caused all
jobs to fire within ~20 minutes of boot and OOM the container. We use
a module-level asyncio.Semaphore to cap concurrent job execution to
MAX_CONCURRENT_JOBS (default 2) and stagger first-runs over ~60 min.
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

# Phase 2: launch + exploit detection
from backend.detection.launch_scorer import run_launch_scorer
from backend.detection.deployer_watcher import run_deployer_watcher
from backend.detection.whale_fresh_wallet import run_whale_fresh_watcher
from backend.detection.pair_watcher import run_pair_watcher
from backend.detection.exploit_watcher import run_exploit_watcher
from backend.detection.operator_graph import run_operator_graph
from backend.detection.bytecode_match import run_bytecode_match
from backend.detection.portfolio_gate import run_portfolio_gate
from backend.detection.launchpad_watcher import run_launchpad_watcher
from backend.detection.treasury_outflow import run_treasury_outflow

logger = logging.getLogger(__name__)


# At most this many jobs may run concurrently. Even though APScheduler's
# AsyncIOScheduler runs everything in one event loop, parallel jobs each
# accumulate Python objects (RPC results, ORM rows, log lists). 2GB
# container OOMs were the result of 5-6 jobs firing simultaneously after
# their staggered first-runs all elapsed in the same 20-min window.
MAX_CONCURRENT_JOBS = 2
_JOB_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_JOBS)


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
    """
    Wallet tracker with logging.

    Notable activities are stored to wallet_activity table only. They do NOT
    fire alerts directly — instead the scorer reads recent activity and
    contributes points to the token score (known_operator_present = +30).
    A Telegram alert is only fired when the combined token score crosses
    the ALERT_THRESHOLD via run_scorer_job.
    """
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


# ─── Phase 2: launch + exploit detection job wrappers ─────────────────

def _wrap_launch_job(job_name: str, coro):
    """
    Wrap a detection coroutine with:
      - semaphore-bounded concurrency (max 2 jobs running at once)
      - scan_log entry on success/error
      - gc.collect() after each run so large transient lists (RPC log
        responses, balance dicts, etc.) get freed before the next job
        kicks off — critical on the 2GB Render container.
    """
    async def _wrapped():
        async with _JOB_SEMAPHORE:
            started = datetime.utcnow()
            try:
                result = await coro()
                details = str(result) if result is not None else "ok"
                log_scan(
                    job_name, "success", details=details,
                    started_at=started, finished_at=datetime.utcnow(),
                )
            except Exception as e:
                logger.error(f"{job_name} failed: {e}")
                log_scan(
                    job_name, "error", error_message=str(e),
                    started_at=started, finished_at=datetime.utcnow(),
                )
            finally:
                gc.collect()
    _wrapped.__name__ = f"run_{job_name}_job"
    return _wrapped


def _wrap_legacy_job(fn):
    """
    Apply the same semaphore+gc discipline to the legacy job functions
    (volume_scanner, profile_checker, wallet_analyzer, etc.) without
    rewriting their bodies. They already do their own scan_log writes,
    so we just guard concurrency + force gc afterwards.
    """
    async def _wrapped():
        async with _JOB_SEMAPHORE:
            try:
                await fn()
            finally:
                gc.collect()
    _wrapped.__name__ = fn.__name__ + "_throttled"
    return _wrapped


run_deployer_watcher_job   = _wrap_launch_job("deployer_watcher",   run_deployer_watcher)
run_whale_fresh_job        = _wrap_launch_job("whale_fresh_watcher", run_whale_fresh_watcher)
run_pair_watcher_job       = _wrap_launch_job("pair_watcher",       run_pair_watcher)
run_launch_scorer_job      = _wrap_launch_job("launch_scorer",      run_launch_scorer)
run_exploit_watcher_job    = _wrap_launch_job("exploit_watcher",    run_exploit_watcher)
run_operator_graph_job     = _wrap_launch_job("operator_graph",     run_operator_graph)
run_bytecode_match_job     = _wrap_launch_job("bytecode_match",     run_bytecode_match)
run_portfolio_gate_job     = _wrap_launch_job("portfolio_gate",     run_portfolio_gate)
run_launchpad_watcher_job  = _wrap_launch_job("launchpad_watcher",  run_launchpad_watcher)
run_treasury_outflow_job   = _wrap_launch_job("treasury_outflow",   run_treasury_outflow)


def setup_scheduler() -> AsyncIOScheduler:
    """
    Configure and return the APScheduler instance.

    Memory + concurrency design:
      - Every job runs through _wrap_legacy_job / _wrap_launch_job which
        share a module-level Semaphore — at most MAX_CONCURRENT_JOBS run
        at the same time. This is the primary OOM mitigation.
      - First-run offsets are spread across ~75 minutes (rather than
        ~20 min) and ordered so the heaviest jobs never overlap their
        first runs with each other.
      - misfire_grace_time is generous so that backed-up jobs simply
        coalesce instead of stacking up after a long pause.
    """
    scheduler = AsyncIOScheduler(job_defaults={
        "max_instances": 1,
        "coalesce": True,
        "misfire_grace_time": 60 * 30,
    })

    now = datetime.utcnow()
    M = lambda mins: now + timedelta(minutes=mins)  # noqa: E731

    # All registrations as (id, name, callable, trigger_kwargs, first_run_offset_min).
    # Offsets are tuned so the heaviest jobs (volume_scanner, wallet_analyzer,
    # bytecode_match, exploit_watcher) don't first-fire in the same window.
    jobs = [
        # Light / fast jobs first — these are cheap to run early
        ("launch_scorer",     "Launch Scorer",           run_launch_scorer_job,     {"minutes": settings.LAUNCH_SCORER_INTERVAL_MIN},        4),
        ("wallet_tracker",    "Wallet Tracker",          _wrap_legacy_job(run_wallet_tracker_job),    {"minutes": settings.WALLET_TRACK_INTERVAL},  6),
        ("pair_watcher",      "Pair Watcher",            run_pair_watcher_job,      {"minutes": settings.PAIR_WATCHER_INTERVAL_MIN},        8),
        ("treasury_outflow",  "Treasury Outflow",        run_treasury_outflow_job,  {"minutes": settings.TREASURY_OUTFLOW_INTERVAL_MIN},   12),

        # Medium jobs — RPC-heavy but bounded
        ("volume_scanner",    "Volume Scanner",          _wrap_legacy_job(run_volume_scanner_job),    {"minutes": settings.VOLUME_SCAN_INTERVAL},   2),
        ("scorer",            "Score Recalculator",      _wrap_legacy_job(run_scorer_job),            {"minutes": settings.SCORE_RECALC_INTERVAL}, 14),
        ("profile_checker",   "Profile Checker",         _wrap_legacy_job(run_profile_checker_job),   {"minutes": settings.PROFILE_CHECK_INTERVAL}, 16),
        ("exchange_flow",     "Exchange Flow Monitor",   _wrap_legacy_job(run_exchange_flow_job),     {"minutes": settings.EXCHANGE_FLOW_INTERVAL}, 18),
        ("deployer_watcher",  "Golden Deployer Watcher", run_deployer_watcher_job,  {"minutes": settings.DEPLOYER_WATCHER_INTERVAL_MIN},   20),
        ("whale_fresh_watcher", "Whale→Fresh Funding",   run_whale_fresh_job,       {"minutes": settings.WHALE_FRESH_WATCHER_INTERVAL_MIN}, 25),

        # Heavy jobs — pushed to later first-runs so they don't pile up at boot
        ("operator_graph",    "Operator Graph",          run_operator_graph_job,    {"minutes": 15}, 30),
        ("launchpad_watcher", "Launchpad Watcher",       run_launchpad_watcher_job, {"minutes": 30}, 35),
        ("exploit_watcher",   "Exploit Watcher",         run_exploit_watcher_job,   {"minutes": settings.EXPLOIT_WATCHER_INTERVAL_MIN},   40),
        ("social_scanner",    "Social Scanner",          _wrap_legacy_job(run_social_scanner_job),    {"minutes": settings.SOCIAL_SCAN_INTERVAL},  45),
        ("portfolio_gate",    "Portfolio Gate",          run_portfolio_gate_job,    {"minutes": 60}, 50),
        ("bytecode_match",    "Bytecode Fingerprint",    run_bytecode_match_job,    {"minutes": 30}, 55),
        ("wallet_analyzer",   "Wallet Analyzer",         _wrap_legacy_job(run_wallet_analyzer_job),   {"minutes": settings.WALLET_ANALYZE_INTERVAL}, 65),
        ("cleanup",           "Cleanup",                 _wrap_legacy_job(run_cleanup_job),           {"hours": 6}, 75),
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
