"""
FastAPI Application — main entry point.

Serves the REST API, React frontend (static files), and runs background
scanner jobs in-process via APScheduler. Single service deployment.
"""

import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from backend.database import Base, SessionLocal, engine
from backend.scheduler import setup_scheduler
from backend.api.dashboard import router as dashboard_router
from backend.api.tokens import router as tokens_router
from backend.api.wallets import router as wallets_router
from backend.api.alerts import router as alerts_router
from backend.api.launches import router as launches_router
from backend.api.exploits import router as exploits_router
from backend.api.diagnostics import router as diagnostics_router

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Create tables on startup
Base.metadata.create_all(bind=engine)


def _widen_legacy_columns() -> None:
    """
    One-shot online migration: widen VARCHAR columns that were initially
    declared too narrow.

    SQLAlchemy's create_all() never alters existing columns, so model
    changes only take effect for fresh databases. These ALTER TABLEs
    are idempotent (Postgres no-op when the type already matches) and
    are safe to keep here permanently.

    Concrete failures this addresses:
      protocol_tvl_snapshots.chain VARCHAR(40) → exceeded by multi-chain
        protocols like "Ethereum,Plasma,Arbitrum,Base,Mantle".
      protocol_tvl_snapshots.protocol_slug VARCHAR(100) → some DeFi Llama
        composite slugs are longer.
      exploit_candidates.{chain,protocol_slug} → same root cause; we
        store composite "{bridge}:{tx_hash}" slugs that exceeded 100.
    """
    from sqlalchemy import text

    statements = [
        # protocol_tvl_snapshots
        "ALTER TABLE protocol_tvl_snapshots ALTER COLUMN chain TYPE VARCHAR(255)",
        "ALTER TABLE protocol_tvl_snapshots ALTER COLUMN protocol_slug TYPE VARCHAR(255)",
        # exploit_candidates
        "ALTER TABLE exploit_candidates ALTER COLUMN chain TYPE VARCHAR(255)",
        "ALTER TABLE exploit_candidates ALTER COLUMN protocol_slug TYPE VARCHAR(255)",
    ]
    with engine.connect() as conn:
        for sql in statements:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception as e:
                # ALTERs on a brand-new table or non-existent column will
                # raise — log and continue.
                logger.info(f"widen migration skipped: {sql} ({e})")


_widen_legacy_columns()


def _cleanup_legacy_data() -> None:
    """
    One-shot data cleanup at boot to scrub the artifacts of the old
    wallet_tracker alert spam (5,406 placeholder alerts, stub watchlist
    entries with bogus cluster counts).

    Idempotent: each run only deletes the specific patterns we know are
    invalid. Real data is never touched.
    """
    from sqlalchemy import or_
    from backend.models.alert import Alert
    from backend.models.flagged_token import FlaggedToken
    from backend.models.watchlist import Watchlist

    db = SessionLocal()
    try:
        # 1. Delete unsent wallet_tracker placeholder alerts
        # ("Pending data enrichment" rows that never reached Telegram).
        deleted_alerts = (
            db.query(Alert)
            .filter(
                Alert.alert_type == "wallet_tracker",
                Alert.telegram_sent.is_(False),
            )
            .delete(synchronize_session=False)
        )

        # 2. Delete watchlist entries that point to stub flagged_tokens
        # (no symbol or no price). These produced the "?" / N/A rows on
        # the dashboard with bogus YES (N) cluster counts.
        stub_addrs_subq = (
            db.query(FlaggedToken.contract_address)
            .filter(
                or_(
                    FlaggedToken.token_symbol.is_(None),
                    FlaggedToken.price_usd.is_(None),
                )
            )
            .subquery()
        )
        deleted_watch = (
            db.query(Watchlist)
            .filter(Watchlist.contract_address.in_(stub_addrs_subq))
            .delete(synchronize_session=False)
        )

        db.commit()
        logger.info(
            f"Startup cleanup: removed {deleted_alerts} placeholder alerts "
            f"and {deleted_watch} stub watchlist entries."
        )
    except Exception as e:
        db.rollback()
        logger.warning(f"Startup cleanup skipped due to error: {e}")
    finally:
        db.close()


_cleanup_legacy_data()

# Track scheduler globally so it can be shut down cleanly
_scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background scanner on startup, stop on shutdown."""
    global _scheduler
    _scheduler = setup_scheduler()
    _scheduler.start()
    logger.info("Background scanner started (in-process)")
    for job in _scheduler.get_jobs():
        logger.info(f"  Job: {job.name} — {job.trigger}")
    yield
    _scheduler.shutdown(wait=False)
    logger.info("Background scanner stopped")


app = FastAPI(
    title="BSC Pump Scanner",
    description="Real-time BSC token pump detection scanner",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routes
app.include_router(dashboard_router)
app.include_router(tokens_router)
app.include_router(wallets_router)
app.include_router(alerts_router)
app.include_router(launches_router)
app.include_router(exploits_router)
app.include_router(diagnostics_router)


@app.get("/api/health")
def health_check():
    """Health check endpoint for Render."""
    jobs = []
    if _scheduler:
        jobs = [{"name": j.name, "next_run": str(j.next_run_time)} for j in _scheduler.get_jobs()]
    return {
        "status": "healthy",
        "service": "bsc-pump-scanner",
        "scanner_active": _scheduler is not None and _scheduler.running,
        "scheduled_jobs": jobs,
    }


# Serve React frontend (production)
frontend_build = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "build")
if os.path.isdir(frontend_build):
    app.mount("/static", StaticFiles(directory=os.path.join(frontend_build, "static")), name="static")

    @app.get("/{full_path:path}")
    async def serve_react(full_path: str):
        """Serve React SPA for any non-API route."""
        file_path = os.path.join(frontend_build, full_path)
        if os.path.isfile(file_path):
            return FileResponse(file_path)
        return FileResponse(os.path.join(frontend_build, "index.html"))
