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
from backend.api.settings import router as settings_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Create tables if they don't exist yet
Base.metadata.create_all(bind=engine)


def _widen_solana_sig_columns() -> None:
    """Solana sigs + mints don't fit BSC-era column widths. Idempotent
    boot migration. Safe to re-run.

    Critical for `alerts`: the Alert row is the dedup source-of-truth
    for Telegram notifications. If an INSERT silently fails because a
    Solana mint (~44 chars) doesn't fit VARCHAR(42), the same
    convergence re-fires every cycle. Ask me how I know."""
    stmts = [
        "ALTER TABLE wallet_funding_edges "
        "ALTER COLUMN tx_hash TYPE VARCHAR(128)",
        "ALTER TABLE wallet_funding_edges "
        "ALTER COLUMN recipient_deploy_tx TYPE VARCHAR(128)",
        "ALTER TABLE alerts "
        "ALTER COLUMN contract_address TYPE VARCHAR(64)",
        "ALTER TABLE alerts "
        "ALTER COLUMN token_symbol TYPE VARCHAR(128)",
        # Sniper profile columns (added post-launch). IF NOT EXISTS is
        # idempotent — safe to re-run every boot.
        "ALTER TABLE solana_wallet_stats "
        "ADD COLUMN IF NOT EXISTS avg_buy_size_usd NUMERIC(20, 2)",
        "ALTER TABLE solana_wallet_stats "
        "ADD COLUMN IF NOT EXISTS avg_exit_multiplier NUMERIC(10, 2)",
        "ALTER TABLE solana_wallet_stats "
        "ADD COLUMN IF NOT EXISTS is_sniper BOOLEAN DEFAULT FALSE",
        "ALTER TABLE solana_wallet_stats "
        "ADD COLUMN IF NOT EXISTS is_dormant BOOLEAN DEFAULT FALSE",
        "ALTER TABLE alerts "
        "ADD COLUMN IF NOT EXISTS mc_at_alert NUMERIC(20, 2)",
        "ALTER TABLE alerts "
        "ADD COLUMN IF NOT EXISTS price_at_alert NUMERIC(30, 12)",
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


def _seed_cex_addresses() -> None:
    """Idempotent: add any CEX addresses from data/cex_addresses.json
    that aren't already in the table."""
    import json as _json
    from backend.database import SessionLocal
    from backend.models.cex_address import CexAddress

    path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", "cex_addresses.json"
    )
    if not os.path.isfile(path):
        return
    try:
        with open(path) as f:
            entries = _json.load(f) or []
    except Exception as e:
        logger.warning(f"CEX seed load failed: {e}")
        return
    if not entries:
        return
    db = SessionLocal()
    try:
        existing = {r[0] for r in db.query(CexAddress.address).all()}
        added = 0
        for e in entries:
            if e.get("address") and e["address"] not in existing:
                db.add(CexAddress(
                    address=e["address"], name=e.get("name") or "Unknown",
                    exchange=e.get("exchange") or None, is_active=True,
                ))
                added += 1
        if added:
            db.commit()
            logger.info(f"CEX seed: added {added} addresses")
    except Exception as e:
        logger.warning(f"CEX seed failed: {e}")
        db.rollback()
    finally:
        db.close()


_seed_cex_addresses()


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
app.include_router(settings_router)


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
