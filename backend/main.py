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


def _widen_legacy_columns() -> dict:
    """
    One-shot online migration: widen VARCHAR columns that were initially
    declared too narrow.

    SQLAlchemy's create_all() never alters existing columns, so model
    changes only take effect for fresh databases. These ALTER TABLEs
    are idempotent (Postgres no-op when the type already matches) and
    are safe to keep here permanently.

    Returns a per-column result dict so the /fix/widen-columns endpoint
    can surface exactly which ALTERs succeeded / failed / were no-ops.
    Every run VERIFIES via information_schema that the column is now at
    the target width — earlier "silent skip" handling was swallowing
    real failures (seen in production: chain stuck at VARCHAR(40)
    despite the ALTER running, causing StringDataRightTruncation on
    every exploit_watcher run).

    Concrete failures this addresses:
      protocol_tvl_snapshots.chain VARCHAR(40) → exceeded by multi-chain
        protocols like "Ethereum,Plasma,Arbitrum,Base,Mantle".
      protocol_tvl_snapshots.protocol_slug VARCHAR(100) → some DeFi Llama
        composite slugs are longer.
      exploit_candidates.{chain,protocol_slug} → same root cause; we
        store composite "{bridge}:{tx_hash}" slugs that exceeded 100.
    """
    from sqlalchemy import text

    targets = [
        # (table, column, target_width)
        ("protocol_tvl_snapshots", "chain",         255),
        ("protocol_tvl_snapshots", "protocol_slug", 255),
        ("exploit_candidates",     "chain",         255),
        ("exploit_candidates",     "protocol_slug", 255),
    ]
    results: dict[str, str] = {}
    with engine.connect() as conn:
        for table, col, width in targets:
            key = f"{table}.{col}"
            sql = f"ALTER TABLE {table} ALTER COLUMN {col} TYPE VARCHAR({width})"
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception as e:
                # Table/column might not exist yet on a brand new DB —
                # note it but don't fail out.
                msg = str(e)[:200]
                results[key] = f"ALTER failed: {msg}"
                logger.warning(
                    f"_widen_legacy_columns: ALTER failed for {key} — {msg}"
                )
                continue

            # VERIFY — query information_schema to confirm width is now >= target.
            try:
                row = conn.execute(
                    text("""
                        SELECT character_maximum_length
                        FROM information_schema.columns
                        WHERE table_name = :t AND column_name = :c
                    """),
                    {"t": table, "c": col},
                ).first()
                if row is None:
                    results[key] = "skipped (table/column missing)"
                elif row[0] is None:
                    results[key] = "skipped (column is not VARCHAR)"
                elif row[0] < width:
                    # This is the FAILURE MODE we care about — ALTER ran
                    # but didn't stick. Previously silent; now loud.
                    results[key] = f"WIDEN DID NOT TAKE EFFECT — still VARCHAR({row[0]})"
                    logger.warning(
                        f"_widen_legacy_columns: {key} still VARCHAR({row[0]}) "
                        f"after ALTER. Run POST /api/diagnostics/fix/widen-columns "
                        f"manually, or investigate DB permissions."
                    )
                else:
                    results[key] = f"ok (VARCHAR({row[0]}))"
            except Exception as e:
                results[key] = f"verify failed: {str(e)[:200]}"
                logger.warning(
                    f"_widen_legacy_columns: verify failed for {key} — {e}"
                )
    return results


_widen_legacy_columns()


def _cleanup_legacy_data() -> None:
    """
    One-shot data cleanup at boot to scrub the artifacts of previous
    buggy behavior:
      - 5k+ placeholder wallet_tracker alerts with telegram_sent=False
      - Stub watchlist entries pointing at un-enriched flagged_tokens
      - 32k+ initial-supply-mint false positives from exploit_watcher
        (every new BSC token creation was being flagged as "abnormal mint")

    Idempotent: each run only deletes the specific patterns we know
    are invalid. Real data is never touched.
    """
    from sqlalchemy import or_
    from backend.models.alert import Alert
    from backend.models.flagged_token import FlaggedToken
    from backend.models.watchlist import Watchlist
    from backend.models.exploit_candidate import ExploitCandidate

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

        # 3. Delete initial-supply-mint false positives. These are
        # ExploitCandidate rows whose evidence.pct_minted >= 99 — those
        # are new token deployments (100% of supply minted in one tx),
        # which is the NORMAL ERC20 creation pattern, not an exploit.
        # We fetch + filter in Python because JSONB cast syntax isn't
        # portable. Bounded by table size and runs once per boot.
        all_abnormal = db.query(ExploitCandidate).filter(
            ExploitCandidate.signal_type == "abnormal_mint",
        ).all()
        bogus_ids = []
        bogus_contracts = set()
        for ec in all_abnormal:
            ev = ec.evidence or {}
            pct = ev.get("pct_minted")
            try:
                if pct is not None and float(pct) >= 99.0:
                    bogus_ids.append(ec.id)
                    if ec.native_token_contract:
                        bogus_contracts.add(ec.native_token_contract.lower())
            except Exception:
                continue
        deleted_mints = 0
        deleted_mint_alerts = 0
        if bogus_ids:
            deleted_mints = db.query(ExploitCandidate).filter(
                ExploitCandidate.id.in_(bogus_ids)
            ).delete(synchronize_session=False)
            deleted_mint_alerts = db.query(Alert).filter(
                Alert.alert_type == "exploit_abnormal_mint",
                Alert.contract_address.in_(bogus_contracts),
            ).delete(synchronize_session=False)

        db.commit()
        logger.info(
            f"Startup cleanup: removed {deleted_alerts} placeholder alerts, "
            f"{deleted_watch} stub watchlist entries, "
            f"{deleted_mints} bogus abnormal-mint exploit rows "
            f"(+ {deleted_mint_alerts} associated alert rows)."
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
