"""
FastAPI entrypoint — Solana cabal tracker.

Serves the REST API, React dashboard, and runs the graph-walk scheduler
in-process. Single-service deployment on Render.
"""

import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy import text as _sql

from backend.database import Base, engine
from backend.scheduler import setup_scheduler
from backend.api.wallets import router as wallets_router
from backend.api.diagnostics import router as diagnostics_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Create tables if they don't exist yet
Base.metadata.create_all(bind=engine)


def _widen_solana_sig_columns() -> None:
    """Solana tx signatures are 87-88 chars; earlier model used
    VARCHAR(80). Idempotent boot migration. Safe to re-run."""
    stmts = [
        "ALTER TABLE wallet_funding_edges "
        "ALTER COLUMN tx_hash TYPE VARCHAR(128)",
        "ALTER TABLE wallet_funding_edges "
        "ALTER COLUMN recipient_deploy_tx TYPE VARCHAR(128)",
    ]
    try:
        with engine.begin() as conn:
            for sql in stmts:
                try:
                    conn.execute(_sql(sql))
                except Exception as e:
                    logger.warning(f"boot migration skipped: {sql} -> {e}")
    except Exception as e:
        logger.warning(f"boot migration failed: {e}")


_widen_solana_sig_columns()


_scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    _scheduler = setup_scheduler()
    _scheduler.start()
    logger.info("Scheduler started")
    for job in _scheduler.get_jobs():
        logger.info(f"  Job: {job.name} — {job.trigger}")
    yield
    _scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")


app = FastAPI(
    title="Solana Cabal Tracker",
    description="Extract cabal wallets from runner CAs; track their rotations.",
    version="3.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(wallets_router)
app.include_router(diagnostics_router)


@app.get("/api/health")
def health_check():
    jobs = []
    if _scheduler:
        jobs = [{"name": j.name, "next_run": str(j.next_run_time)}
                for j in _scheduler.get_jobs()]
    return {
        "status": "healthy",
        "service": "solana-cabal-tracker",
        "scheduler_active": _scheduler is not None and _scheduler.running,
        "scheduled_jobs": jobs,
    }


# Serve React frontend (production build)
frontend_build = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "frontend", "build"
)
if os.path.isdir(frontend_build):
    app.mount("/static",
              StaticFiles(directory=os.path.join(frontend_build, "static")),
              name="static")

    @app.get("/{full_path:path}")
    async def serve_react(full_path: str):
        file_path = os.path.join(frontend_build, full_path)
        if os.path.isfile(file_path):
            return FileResponse(file_path)
        return FileResponse(os.path.join(frontend_build, "index.html"))
