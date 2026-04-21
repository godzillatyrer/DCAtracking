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


def _cleanup_legacy_data() -> None:
    """
    One-shot data cleanup at boot to scrub the artifacts of previous
    buggy behavior:
      - 5k+ placeholder wallet_tracker alerts with telegram_sent=False
      - Stub watchlist entries pointing at un-enriched flagged_tokens

    Idempotent: each run only deletes the specific patterns we know
    are invalid. Real data is never touched.
    """
    from sqlalchemy import or_
    from backend.models.alert import Alert
    from backend.models.flagged_token import FlaggedToken
    from backend.models.watchlist import Watchlist

    db = SessionLocal()
    try:
        deleted_alerts = (
            db.query(Alert)
            .filter(
                Alert.alert_type == "wallet_tracker",
                Alert.telegram_sent.is_(False),
            )
            .delete(synchronize_session=False)
        )

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
            f"Startup cleanup: removed {deleted_alerts} placeholder alerts, "
            f"{deleted_watch} stub watchlist entries."
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
    description="Real-time pump-and-dump detection + cabal wallet tracking",
    version="2.0.0",
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
