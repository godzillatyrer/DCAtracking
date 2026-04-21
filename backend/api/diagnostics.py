"""
Diagnostics router — API health, scheduler freshness, schema drift.

Endpoints:
  GET  /api/diagnostics/apis           — per-provider reachability + latency
  GET  /api/diagnostics/health-audit   — structured issue list for routines
  POST /api/diagnostics/fix/widen-columns   — widen Solana VARCHAR columns
  POST /api/diagnostics/fix/cleanup-orphans — kept for compat (no-op)
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, text as _text
from sqlalchemy.orm import Session

from backend.clients import birdeye, defillama, geckoterminal, gmgn, helius
from backend.config import settings
from backend.database import engine, get_db
from backend.models.scan_log import ScanLog

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])


# Expected max interval (minutes) for each scheduled job. If the latest
# scan_log entry for a job is older than 2× this, we flag it stale.
_JOB_INTERVAL_MIN = {
    "solana_graph_walk": 15,
}


# --- In-process cache ------------------------------------------------
_CACHE: dict[str, tuple[float, dict]] = {}
_TTL = 300.0


def _cached(key: str):
    entry = _CACHE.get(key)
    if not entry:
        return None
    ts, value = entry
    if time.time() - ts > _TTL:
        return None
    return value


def _store(key: str, value: dict):
    _CACHE[key] = (time.time(), value)


async def _timed(awaitable):
    t0 = time.monotonic()
    try:
        result = await awaitable
        ok = result is not None and result != [] and result != {}
        return ok, int((time.monotonic() - t0) * 1000), result, None
    except Exception as e:
        return False, int((time.monotonic() - t0) * 1000), None, str(e)[:200]


# ─── Per-provider health checks ────────────────────────────────────────

async def _check_helius() -> dict:
    if not helius.configured:
        return {"configured": False, "status": "not_configured",
                "detail": "HELIUS_API_KEY not set"}
    ok, ms, _, err = await _timed(
        helius._rpc("getSlot", [])
    )
    return {
        "configured": True,
        "status": "ok" if ok else "error",
        "latency_ms": ms,
        "detail": "reachable" if ok else (err or "empty response"),
    }


async def _check_gmgn() -> dict:
    # Cheap ping: fetch holders for SOL itself (always indexed)
    ok, ms, _, err = await _timed(
        gmgn.token_holders("So11111111111111111111111111111111111111112", limit=5)
    )
    return {
        "configured": True,
        "status": "ok" if ok else "error",
        "latency_ms": ms,
        "detail": "reachable" if ok else (err or "empty — likely Cloudflare"),
    }


async def _check_birdeye() -> dict:
    if not birdeye.configured:
        return {"configured": False, "status": "not_configured",
                "detail": "BIRDEYE_API_KEY not set"}
    ok, ms, _, err = await _timed(
        birdeye.token_overview("So11111111111111111111111111111111111111112")
    )
    return {
        "configured": True,
        "status": "ok" if ok else "error",
        "latency_ms": ms,
        "detail": "reachable" if ok else (err or "empty response"),
    }


async def _check_defillama() -> dict:
    ok, ms, _, err = await _timed(
        defillama.current_prices(["coingecko:solana"])
    )
    return {
        "configured": True,
        "status": "ok" if ok else "error",
        "latency_ms": ms,
        "detail": "reachable" if ok else (err or "empty response"),
    }


async def _check_geckoterminal() -> dict:
    ok, ms, _, err = await _timed(
        geckoterminal.token_info(
            "solana", "So11111111111111111111111111111111111111112"
        )
    )
    return {
        "configured": True,
        "status": "ok" if ok else "error",
        "latency_ms": ms,
        "detail": "reachable" if ok else (err or "empty response"),
    }


async def _check_telegram() -> dict:
    import httpx
    if not settings.TELEGRAM_BOT_TOKEN:
        return {"configured": False, "status": "not_configured",
                "detail": "TELEGRAM_BOT_TOKEN not set"}
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/getMe"
            )
        ms = int((time.monotonic() - t0) * 1000)
        if resp.status_code == 200 and resp.json().get("ok"):
            return {"configured": True, "status": "ok", "latency_ms": ms,
                    "detail": "bot reachable"}
        return {"configured": True, "status": "error", "latency_ms": ms,
                "detail": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"configured": True, "status": "error",
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "detail": str(e)[:200]}


PROVIDERS = [
    ("Helius",        "HELIUS_API_KEY",   "Solana RPC — graph walk, tx history",       _check_helius),
    ("GMGN",          "GMGN_API_KEY",     "Solana smart money top_holders/top_traders", _check_gmgn),
    ("Birdeye",       "BIRDEYE_API_KEY",  "Solana token metadata / price fallback",    _check_birdeye),
    ("DeFi Llama",    "(no key)",         "SOL price lookup",                           _check_defillama),
    ("GeckoTerminal", "(no key)",         "Token price lookup",                         _check_geckoterminal),
    ("Telegram",      "TELEGRAM_BOT_TOKEN", "Alert delivery",                           _check_telegram),
]


@router.get("/apis")
async def api_status(force: int = Query(0)):
    """Per-provider health snapshot."""
    cache_key = "api_status"
    if not force:
        cached = _cached(cache_key)
        if cached is not None:
            return cached

    checks = await asyncio.gather(
        *[fn() for _, _, _, fn in PROVIDERS], return_exceptions=True
    )

    providers = []
    for (name, key_name, purpose, _), result in zip(PROVIDERS, checks):
        if isinstance(result, Exception):
            result = {"configured": False, "status": "error",
                      "detail": str(result)[:200]}
        providers.append({
            "name": name,
            "key_env": key_name,
            "purpose": purpose,
            **result,
        })

    payload = {"checked_at": datetime.utcnow().isoformat(), "providers": providers}
    _store(cache_key, payload)
    return payload


# ─── Health audit (structured issues) ───────────────────────────────

def _check_scheduler_jobs(db: Session) -> list[dict]:
    """Flag jobs whose latest scan_log is too old or errored."""
    issues = []
    now = datetime.utcnow()
    for job, interval_min in _JOB_INTERVAL_MIN.items():
        last = (
            db.query(ScanLog)
            .filter(ScanLog.job_name == job)
            .order_by(desc(ScanLog.started_at))
            .first()
        )
        if last is None:
            issues.append({
                "id": f"job_never_ran:{job}",
                "severity": "warning",
                "category": "scheduler",
                "title": f"Job '{job}' has never run",
                "details": {"job": job},
                "likely_fix": "Check that HELIUS_API_KEY is set; the job "
                              "logs and skips without it.",
                "files_to_check": [f"backend/detection/{job}.py"],
                "auto_fix_url": None,
            })
            continue

        stale_after = timedelta(minutes=interval_min * 2)
        if last.started_at and (now - last.started_at) > stale_after:
            issues.append({
                "id": f"job_stale:{job}",
                "severity": "critical",
                "category": "scheduler",
                "title": f"Job '{job}' last ran "
                         f"{int((now - last.started_at).total_seconds() // 60)} min ago",
                "details": {
                    "job": job,
                    "last_run": last.started_at.isoformat(),
                    "expected_interval_min": interval_min,
                },
                "likely_fix": "Check Render logs for OOM / scheduler crash.",
                "files_to_check": ["backend/scheduler.py"],
                "auto_fix_url": None,
            })

        if last.status == "error":
            issues.append({
                "id": f"job_error:{job}",
                "severity": "warning",
                "category": "scheduler",
                "title": f"Job '{job}' last run errored",
                "details": {
                    "job": job,
                    "last_run": last.started_at.isoformat() if last.started_at else None,
                    "last_error": (last.error_message or "")[:500],
                },
                "likely_fix": "Read the error and trace to the module.",
                "files_to_check": [f"backend/detection/{job}.py"],
                "auto_fix_url": None,
            })
    return issues


def _check_schema_drift() -> list[dict]:
    """Flag Solana-sig columns that are still too narrow."""
    # (table, column, minimum_width). Solana base58 signatures are 87-88
    # chars; anything < 88 will StringDataRightTruncate on insert.
    expected = [
        ("wallet_funding_edges", "tx_hash", 128),
        ("wallet_funding_edges", "recipient_deploy_tx", 128),
    ]
    issues = []
    try:
        with engine.connect() as conn:
            for table, col, want in expected:
                row = conn.execute(
                    _text("""
                        SELECT character_maximum_length
                        FROM information_schema.columns
                        WHERE table_name = :t AND column_name = :c
                    """),
                    {"t": table, "c": col},
                ).first()
                if row is None:
                    continue
                actual = row[0]
                if actual is None or actual >= want:
                    continue
                issues.append({
                    "id": f"schema_drift:{table}.{col}",
                    "severity": "critical",
                    "category": "schema",
                    "title": f"Column {table}.{col} is VARCHAR({actual}), expected >= {want}",
                    "details": {
                        "table": table, "column": col,
                        "actual_width": actual, "expected_width": want,
                    },
                    "likely_fix": "POST to auto_fix_url to widen.",
                    "files_to_check": ["backend/models/wallet_funding_graph.py"],
                    "auto_fix_url": "/api/diagnostics/fix/widen-columns",
                })
    except Exception as e:
        issues.append({
            "id": "schema_check_error",
            "severity": "warning",
            "category": "schema",
            "title": "Failed to inspect schema",
            "details": {"error": str(e)[:300]},
            "likely_fix": "Manual investigation required.",
            "files_to_check": ["backend/api/diagnostics.py"],
            "auto_fix_url": None,
        })
    return issues


@router.get("/health-audit")
def health_audit(db: Session = Depends(get_db)):
    """Structured audit. Pairs with scripts/routine_health_check.py."""
    issues: list[dict] = []
    issues.extend(_check_scheduler_jobs(db))
    issues.extend(_check_schema_drift())

    critical = sum(1 for i in issues if i["severity"] == "critical")
    warning = sum(1 for i in issues if i["severity"] == "warning")
    info = sum(1 for i in issues if i["severity"] == "info")

    return {
        "healthy": critical == 0,
        "checked_at": datetime.utcnow().isoformat(),
        "summary": {
            "total": len(issues),
            "critical": critical,
            "warning": warning,
            "info": info,
        },
        "issues": issues,
    }


# ─── Safe auto-fix endpoints ─────────────────────────────────────────

@router.post("/fix/widen-columns")
def fix_widen_columns():
    """Widen Solana-sig columns to VARCHAR(128). Idempotent."""
    stmts = [
        "ALTER TABLE wallet_funding_edges "
        "ALTER COLUMN tx_hash TYPE VARCHAR(128)",
        "ALTER TABLE wallet_funding_edges "
        "ALTER COLUMN recipient_deploy_tx TYPE VARCHAR(128)",
    ]
    applied = []
    with engine.begin() as conn:
        for sql in stmts:
            try:
                conn.execute(_text(sql))
                applied.append(sql)
            except Exception as e:
                logger.warning(f"widen-columns skipped: {sql} -> {e}")
    return {"status": "ok", "applied": applied}


@router.post("/fix/cleanup-orphans")
def fix_cleanup_orphans():
    """Kept for backwards-compat with the routine health-check. No-op
    under the Solana-only schema."""
    return {"status": "ok", "message": "nothing to clean"}
