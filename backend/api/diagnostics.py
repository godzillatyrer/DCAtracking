"""
Diagnostics router — surfaces health for every external API in a single
endpoint so the UI can render an "API Status" dashboard.

Each check:
  - reports whether the API key is configured
  - runs a cheap live ping (when configured)
  - returns latency + a short "ok / error" detail
  - lists which detection modules depend on this API

Results are cached in-process for ~60s to avoid hammering APIs on every
frontend refresh. The cache is bypassed when ?force=1 is passed.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc
from sqlalchemy.orm import Session

from backend.clients import (
    birdeye,
    defillama,
    geckoterminal,
    gmgn,
    helius,
    nansen,
)
from backend.config import settings
from backend.database import engine, get_db
from backend.models.scan_log import ScanLog

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])


# Last-error lookup — we surface the most recent failing scan_log per job
# as a clickable row on the API Status page so you can see WHY something
# is unhealthy without trawling scan logs.
ERROR_JOBS_BY_API = {
    "MegaNode": ["volume_scanner", "profile_checker", "wallet_tracker", "wallet_analyzer", "exchange_flow", "deployer_watcher", "whale_fresh_watcher", "pair_watcher", "exploit_watcher"],
    "Arkham": ["wallet_seeder", "portfolio_gate"],
    "DeFi Llama": ["exploit_watcher"],
    "GeckoTerminal": ["pair_watcher", "solana_pair_watcher"],
    "Helius": ["solana_pair_watcher", "solana_deployer_watcher", "solana_whale_fresh"],
    "Nansen": ["portfolio_gate"],
    "GMGN": ["solana_pair_watcher"],
    "Telegram": ["wallet_tracker", "scorer", "launch_scorer"],
    "Anthropic": ["scorer", "launch_scorer"],
}


# --- In-process cache ------------------------------------------------
#
# Cached for 5 minutes so the API Status page + routine audits don't
# themselves contribute meaningful traffic to rate-limited providers
# (esp. GeckoTerminal's 30 req/min). `?force=1` bypasses. Previously
# 60s which was enough to start tripping the GT limit alongside the
# real pair_watcher traffic.
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


# --- Individual health checks --------------------------------------------

async def _timed(coro) -> tuple[bool, int, str, str | None]:
    """Run a coroutine, return (ok, latency_ms, detail, error)."""
    t0 = time.time()
    try:
        result = await coro
        ms = int((time.time() - t0) * 1000)
        if result is None or result is False:
            return (False, ms, "", "empty/null response")
        return (True, ms, str(result)[:120], None)
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        return (False, ms, "", str(e)[:200])


async def _check_meganode() -> dict:
    """Check BSC RPC by pulling the latest block number."""
    from backend.bscscan_client import _rpc_call, _hex_to_int
    if not settings.MEGANODE_API_KEY:
        return {"configured": False, "status": "not_configured", "detail": "MEGANODE_API_KEY is empty"}
    ok, ms, detail, err = await _timed(_rpc_call("eth_blockNumber", []))
    if ok:
        try:
            block = _hex_to_int(detail.replace('"', '').strip()) if isinstance(detail, str) else 0
        except Exception:
            block = 0
        return {"configured": True, "status": "ok", "latency_ms": ms, "detail": f"latest block: {block}" if block else detail}
    return {"configured": True, "status": "error", "latency_ms": ms, "error": err}


async def _check_arkham() -> dict:
    if not settings.ARKHAM_API_KEY:
        return {"configured": False, "status": "not_configured", "detail": "ARKHAM_API_KEY is empty"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            t0 = time.time()
            resp = await client.get(
                f"{settings.ARKHAM_BASE_URL}/intelligence/address/0x28c6c06298d514db089934071355e5743bf21d60",
                headers={"API-Key": settings.ARKHAM_API_KEY},
            )
            ms = int((time.time() - t0) * 1000)
            if resp.status_code == 200:
                return {"configured": True, "status": "ok", "latency_ms": ms, "detail": "entity lookup ok"}
            return {"configured": True, "status": "error", "latency_ms": ms, "error": f"HTTP {resp.status_code}: {resp.text[:150]}"}
    except Exception as e:
        return {"configured": True, "status": "error", "error": str(e)[:200]}


async def _http_probe(
    url: str,
    headers: dict | None = None,
    *,
    method: str = "GET",
    json_body: dict | None = None,
    expect_json: bool = True,
) -> dict:
    """
    Make a single HTTP request with full error detail. Replaces the
    'empty/null' output of the client wrappers — we need to see HTTP
    status, body preview, exception type for triage. Supports POST
    because Nansen's data endpoints are POST-only.
    """
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            if method.upper() == "POST":
                resp = await client.post(url, headers=headers or {}, json=json_body or {})
            else:
                resp = await client.get(url, headers=headers or {})
            ms = int((time.time() - t0) * 1000)
            preview = resp.text[:180]
            if resp.status_code == 200:
                if expect_json:
                    try:
                        data = resp.json()
                        return {"ok": True, "ms": ms, "status": 200, "data": data, "preview": preview}
                    except Exception as e:
                        return {
                            "ok": False, "ms": ms, "status": 200,
                            "error": f"200 OK but response isn't JSON: {e}",
                            "preview": preview,
                        }
                return {"ok": True, "ms": ms, "status": 200, "data": resp.text, "preview": preview}
            return {
                "ok": False, "ms": ms, "status": resp.status_code,
                "error": f"HTTP {resp.status_code}",
                "preview": preview,
            }
    except httpx.TimeoutException:
        return {"ok": False, "ms": int((time.time() - t0) * 1000), "error": "timeout"}
    except Exception as e:
        return {"ok": False, "ms": int((time.time() - t0) * 1000), "error": str(e)[:180]}


async def _check_defillama() -> dict:
    r = await _http_probe(f"{defillama.base}/chains")
    if r["ok"]:
        count = len(r["data"]) if isinstance(r["data"], list) else None
        return {
            "configured": True, "status": "ok", "latency_ms": r["ms"],
            "detail": f"chains list ok ({count} chains)" if count else "chains list ok",
        }
    return {
        "configured": True, "status": "error", "latency_ms": r["ms"],
        "error": f"{r.get('error')} — preview: {r.get('preview','')}".strip(),
        "detail": "public API (no key needed)",
    }


async def _check_geckoterminal() -> dict:
    # GeckoTerminal recommends an explicit Accept header for API stability
    headers = {"Accept": "application/json;version=20230302"}
    r = await _http_probe(f"{geckoterminal.base}/networks", headers=headers)
    if r["ok"]:
        data = r["data"] if isinstance(r["data"], dict) else {}
        count = len((data.get("data") or []))
        return {
            "configured": True, "status": "ok", "latency_ms": r["ms"],
            "detail": f"networks list ok ({count} networks)",
        }
    # Rate limit is worth calling out so the user doesn't think it's broken
    status = r.get("status")
    if status == 429:
        return {
            "configured": True, "status": "error", "latency_ms": r["ms"],
            "error": "HTTP 429 — GeckoTerminal rate-limited. Limit is 30 req/min on the free tier.",
            "detail": "public API (no key needed)",
        }
    return {
        "configured": True, "status": "error", "latency_ms": r["ms"],
        "error": f"{r.get('error')} — preview: {r.get('preview','')[:120]}".strip(),
        "detail": "public API (no key needed)",
    }


async def _check_helius() -> dict:
    if not helius.configured:
        return {"configured": False, "status": "not_configured", "detail": "HELIUS_API_KEY is empty"}
    ok, ms, detail, err = await _timed(helius.get_slot())
    if ok:
        return {"configured": True, "status": "ok", "latency_ms": ms, "detail": f"slot: {detail}"}
    return {"configured": True, "status": "error", "latency_ms": ms, "error": err}


async def _check_nansen() -> dict:
    """
    Probe Nansen with the correct transport. Per docs.nansen.ai:
      - Base URL: https://api.nansen.ai/api/v1 (beta deprecated 2025-10-01)
      - Header:   apikey (lowercase)
      - Method:   POST with JSON body — GETs always 404
    """
    if not nansen.configured:
        return {
            "configured": False, "status": "not_configured",
            "detail": "NANSEN_API_KEY is empty (optional, paid $150+/mo)",
        }

    probe_addr = "0x28c6c06298d514db089934071355e5743bf21d60"  # Binance hot wallet
    base = nansen.base.rstrip("/")
    h_apikey = {
        "apikey": nansen.key,
        "accept": "application/json",
        "Content-Type": "application/json",
    }

    # Documented POST endpoints in priority order.
    candidates = [
        (f"{base}/smart-money/holdings",
         {"chains": ["ethereum"], "pagination": {"page": 1, "per_page": 1}},
         "smart-money/holdings (docs quickstart)"),
        (f"{base}/smart-money/netflow",
         {"chains": ["ethereum"], "pagination": {"page": 1, "per_page": 1}},
         "smart-money/netflow"),
        (f"{base}/profiler/address/balances",
         {"addresses": [probe_addr], "chains": ["ethereum"]},
         "profiler/address/balances"),
        (f"{base}/profiler/address/transactions",
         {"address": probe_addr, "chains": ["ethereum"],
          "pagination": {"page": 1, "per_page": 1}},
         "profiler/address/transactions"),
    ]

    attempts = []
    for url, body, label in candidates:
        r = await _http_probe(url, headers=h_apikey, method="POST", json_body=body)
        attempts.append({
            "endpoint": label,
            "url": url,
            "status": r.get("status"),
            "error": r.get("error"),
            "preview": r.get("preview", "")[:160],
        })
        if r["ok"]:
            return {
                "configured": True, "status": "ok", "latency_ms": r["ms"],
                "detail": f"{label} ok",
                "working_url": url,
            }

    return {
        "configured": True, "status": "error",
        "error": (
            f"All {len(candidates)} documented endpoints failed — "
            "see per-endpoint status codes below."
        ),
        "attempts": attempts,
        "hint": (
            "status=401 → NANSEN_API_KEY is invalid or expired. "
            "status=403 → endpoint not included in your plan "
            "(e.g. Standard vs Pro). "
            "status=404 → NANSEN_BASE_URL wrong; correct value is "
            "https://api.nansen.ai/api/v1. "
            "status=429 → rate-limited (20/s, 500/min)."
        ),
    }


async def _check_gmgn() -> dict:
    # GMGN public tier — no key needed. Health-check via the top_holders endpoint
    # on a well-known Solana token (SOL wrapped = So11…).
    ok, ms, _, err = await _timed(
        gmgn.token_holders("So11111111111111111111111111111111111111112", limit=1)
    )
    if ok:
        return {"configured": True, "status": "ok", "latency_ms": ms, "detail": "top_holders ok (public tier)"}
    return {"configured": True, "status": "error", "latency_ms": ms, "error": err, "detail": "public endpoint — no key needed"}


async def _check_birdeye() -> dict:
    if not birdeye.configured:
        return {"configured": False, "status": "not_configured", "detail": "BIRDEYE_API_KEY is empty (skipped — not required)"}
    ok, ms, _, err = await _timed(birdeye.token_overview("So11111111111111111111111111111111111111112"))
    if ok:
        return {"configured": True, "status": "ok", "latency_ms": ms, "detail": "token_overview ok"}
    return {"configured": True, "status": "error", "latency_ms": ms, "error": err}


async def _check_telegram() -> dict:
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        return {"configured": False, "status": "not_configured", "detail": "TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID is empty"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            t0 = time.time()
            resp = await client.get(
                f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/getMe"
            )
            ms = int((time.time() - t0) * 1000)
            if resp.status_code == 200 and resp.json().get("ok"):
                me = resp.json()["result"]
                return {"configured": True, "status": "ok", "latency_ms": ms, "detail": f"bot: @{me.get('username', '?')}"}
            return {"configured": True, "status": "error", "latency_ms": ms, "error": f"HTTP {resp.status_code}: {resp.text[:150]}"}
    except Exception as e:
        return {"configured": True, "status": "error", "error": str(e)[:200]}


async def _check_anthropic() -> dict:
    if not settings.ANTHROPIC_API_KEY:
        return {"configured": False, "status": "not_configured", "detail": "ANTHROPIC_API_KEY is empty"}
    # Anthropic doesn't have a free "ping" endpoint — we just report configured.
    # Real health shows up when a briefing generation fails (see scan_logs).
    return {"configured": True, "status": "ok", "detail": "key set (live-checked on next briefing)"}


# --- Orchestrator --------------------------------------------------------

ALL_CHECKS = [
    ("MegaNode",       "MEGANODE_API_KEY",  "BSC RPC — all BSC scanners/watchers",                          _check_meganode),
    ("Arkham",         "ARKHAM_API_KEY",    "Entity labels — wallet seeder, portfolio gate",                _check_arkham),
    ("DeFi Llama",     "-",                 "TVL + prices — exploit watcher, golden deployer seeder",       _check_defillama),
    ("GeckoTerminal",  "-",                 "New pools — pair watcher",                                     _check_geckoterminal),
    ("Helius",         "HELIUS_API_KEY",    "Solana RPC — required for solana_pair / solana_deployer / solana_whale_fresh", _check_helius),
    ("Nansen",         "NANSEN_API_KEY",    "Smart Money labels — portfolio gate, whale enrichment (paid)", _check_nansen),
    ("GMGN",           "-",                 "Solana smart-money — Solana holder analysis",                  _check_gmgn),
    ("Birdeye",        "BIRDEYE_API_KEY",   "Solana token metadata — skipped, use DEX Screener instead",    _check_birdeye),
    ("Telegram",       "TELEGRAM_BOT_TOKEN","Alert delivery",                                               _check_telegram),
    ("Anthropic",      "ANTHROPIC_API_KEY", "AI briefings for alerts",                                      _check_anthropic),
]


def _last_error_for(db: Session, api_name: str) -> dict | None:
    jobs = ERROR_JOBS_BY_API.get(api_name) or []
    if not jobs:
        return None
    row = (
        db.query(ScanLog)
        .filter(
            ScanLog.job_name.in_(jobs),
            ScanLog.status == "error",
            ScanLog.started_at >= datetime.utcnow() - timedelta(hours=24),
        )
        .order_by(desc(ScanLog.started_at))
        .first()
    )
    if not row:
        return None
    return {
        "job": row.job_name,
        "at": row.started_at.isoformat() if row.started_at else None,
        "error": (row.error_message or "")[:300],
    }


@router.get("/apis")
async def api_status(
    force: bool = Query(False),
    db: Session = Depends(get_db),
):
    """Aggregate health check for every external API + last job error per API."""
    cache_key = "apis"
    cached = None if force else _cached(cache_key)
    if cached:
        return cached

    # Run all checks concurrently
    coros = [check() for _, _, _, check in ALL_CHECKS]
    results = await asyncio.gather(*coros, return_exceptions=True)

    apis = []
    for (name, env_key, role, _), result in zip(ALL_CHECKS, results):
        if isinstance(result, Exception):
            entry = {"status": "error", "error": str(result)[:200]}
        else:
            entry = result
        apis.append({
            "name": name,
            "env_key": env_key,
            "role": role,
            "configured": entry.get("configured", False),
            "status": entry.get("status", "unknown"),
            "latency_ms": entry.get("latency_ms"),
            "detail": entry.get("detail"),
            "error": entry.get("error"),
            # Extra diagnostic fields some checks populate — e.g. Nansen
            # returns `attempts` (per-URL probe matrix) + `hint` to help
            # the user diagnose plan/URL/auth issues.
            "attempts": entry.get("attempts"),
            "hint": entry.get("hint"),
            "working_url": entry.get("working_url"),
            "last_job_error": _last_error_for(db, name),
            "checked_at": datetime.utcnow().isoformat(),
        })

    summary = {
        "total": len(apis),
        "ok": sum(1 for a in apis if a["status"] == "ok"),
        "errors": sum(1 for a in apis if a["status"] == "error"),
        "not_configured": sum(1 for a in apis if a["status"] == "not_configured"),
    }

    payload = {"apis": apis, "summary": summary, "checked_at": datetime.utcnow().isoformat()}
    _store(cache_key, payload)
    return payload


# ─── Routine-friendly health audit ────────────────────────────────────
#
# /health-audit returns a single structured JSON doc designed to be
# consumed by automated agents (Claude Code routines, monitoring tools).
# Each issue has:
#   id              stable identifier — agents can dedupe across runs
#   severity        critical | warning | info
#   category        scheduler | api | schema | data_freshness | alerts
#   title           short human-readable summary
#   details         structured fields the agent can act on
#   likely_fix      prose describing what to do
#   files_to_check  relative paths an agent should inspect first
#   auto_fix_url    if set, a POST to this URL applies the fix
#
# The agent should:
#   1. Run this endpoint.
#   2. For any issue with `auto_fix_url`, POST to it and re-run audit.
#   3. For remaining issues, investigate and open a PR.
#   4. Never modify main directly.


# Expected interval per scheduler job (for stale detection).
_JOB_INTERVAL_MIN = {
    "volume_scanner":           settings.VOLUME_SCAN_INTERVAL,
    "profile_checker":          settings.PROFILE_CHECK_INTERVAL,
    "wallet_analyzer":          settings.WALLET_ANALYZE_INTERVAL,
    "exchange_flow":            settings.EXCHANGE_FLOW_INTERVAL,
    "social_scanner":           settings.SOCIAL_SCAN_INTERVAL,
    "wallet_tracker":           settings.WALLET_TRACK_INTERVAL,
    "scorer":                   settings.SCORE_RECALC_INTERVAL,
    "cleanup":                  60 * 6,   # 6h
    "pair_watcher":             settings.PAIR_WATCHER_INTERVAL_MIN,
    "deployer_watcher":         settings.DEPLOYER_WATCHER_INTERVAL_MIN,
    "whale_fresh_watcher":      settings.WHALE_FRESH_WATCHER_INTERVAL_MIN,
    "launch_scorer":            settings.LAUNCH_SCORER_INTERVAL_MIN,
    "exploit_watcher":          settings.EXPLOIT_WATCHER_INTERVAL_MIN,
    "operator_graph":           15,
    "bytecode_match":           30,
    "portfolio_gate":           60,
    "launchpad_watcher":        30,
    "treasury_outflow":         settings.TREASURY_OUTFLOW_INTERVAL_MIN,
    "solana_pair_watcher":      settings.SOLANA_PAIR_WATCHER_INTERVAL_MIN,
    "solana_deployer_watcher":  settings.SOLANA_DEPLOYER_WATCHER_INTERVAL_MIN,
    "solana_whale_fresh":       settings.SOLANA_WHALE_FRESH_INTERVAL_MIN,
}


def _check_scheduler_jobs(db: Session) -> list[dict]:
    """Flag jobs that are stale, erroring, or never-ran."""
    from backend.models.scan_log import ScanLog as _SL

    issues = []
    now = datetime.utcnow()

    for job, interval in _JOB_INTERVAL_MIN.items():
        last = (
            db.query(_SL)
            .filter(_SL.job_name == job)
            .order_by(_SL.started_at.desc())
            .first()
        )
        stale_cutoff = now - timedelta(minutes=interval * 3)  # 3× grace

        if last is None:
            issues.append({
                "id": f"job_never_ran:{job}",
                "severity": "warning",
                "category": "scheduler",
                "title": f"Job '{job}' has never run",
                "details": {
                    "job": job,
                    "expected_interval_minutes": interval,
                    "cause": "missing_key_or_not_yet_scheduled",
                },
                "likely_fix": (
                    "If job requires an API key (e.g. solana_* needs HELIUS_API_KEY, "
                    "portfolio_gate needs NANSEN_API_KEY), this is expected. "
                    "Otherwise check scheduler.py to confirm registration."
                ),
                "files_to_check": ["backend/scheduler.py"],
                "auto_fix_url": None,
            })
            continue

        if last.started_at and last.started_at < stale_cutoff:
            hours_ago = (now - last.started_at).total_seconds() / 3600
            issues.append({
                "id": f"job_stale:{job}",
                "severity": "critical",
                "category": "scheduler",
                "title": f"Job '{job}' last ran {hours_ago:.1f}h ago (expected every {interval}m)",
                "details": {
                    "job": job,
                    "last_run": last.started_at.isoformat(),
                    "expected_interval_minutes": interval,
                    "hours_since_last_run": round(hours_ago, 1),
                    "last_status": last.status,
                    "last_error": (last.error_message or "")[:300] if last.status == "error" else None,
                },
                "likely_fix": (
                    "Either the scheduler crashed (check Render logs for OOM), "
                    "or this specific job is erroring — look at the last_error field."
                ),
                "files_to_check": [
                    "backend/scheduler.py",
                    f"backend/detection/{job}.py",
                    f"backend/scanners/{job}.py",
                    f"backend/trackers/{job}.py",
                ],
                "auto_fix_url": None,
            })
            continue

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
                "likely_fix": "Read the error message and trace to the offending module.",
                "files_to_check": [
                    f"backend/detection/{job}.py",
                    f"backend/scanners/{job}.py",
                    f"backend/trackers/{job}.py",
                ],
                "auto_fix_url": None,
            })

    return issues


def _check_schema_drift(db: Session) -> list[dict]:
    """
    Compare live column widths in Postgres to the widths we expect after
    _widen_legacy_columns() has run. If any column is still narrow, flag
    it so the routine can POST to /fix/widen-columns and retry.
    """
    from sqlalchemy import text as _text

    expected = [
        ("protocol_tvl_snapshots", "chain",         255),
        ("protocol_tvl_snapshots", "protocol_slug", 255),
        ("exploit_candidates",     "chain",         255),
        ("exploit_candidates",     "protocol_slug", 255),
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
                    continue  # table doesn't exist yet
                actual = row[0]
                if actual is None or actual >= want:
                    continue
                issues.append({
                    "id": f"schema_drift:{table}.{col}",
                    "severity": "critical",
                    "category": "schema",
                    "title": f"Column {table}.{col} is VARCHAR({actual}), expected >= {want}",
                    "details": {
                        "table": table,
                        "column": col,
                        "actual_width": actual,
                        "expected_width": want,
                    },
                    "likely_fix": "POST to auto_fix_url to re-run _widen_legacy_columns.",
                    "files_to_check": ["backend/main.py"],
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


def _check_data_freshness(db: Session) -> list[dict]:
    """Flag pipelines that have gone quiet even though jobs appear to be running."""
    from backend.models.flagged_token import FlaggedToken
    from backend.models.launch_candidate import LaunchCandidate
    from backend.models.scan_log import ScanLog as _SL

    issues = []
    now = datetime.utcnow()

    # Has volume_scanner logged anything in the last hour?
    vs_recent = db.query(_SL).filter(
        _SL.job_name == "volume_scanner",
        _SL.started_at >= now - timedelta(hours=1),
    ).first()

    if vs_recent is not None:
        # Volume scanner is running — has it flagged ANY tokens in 24h?
        latest_flag = db.query(FlaggedToken).order_by(
            FlaggedToken.first_flagged_at.desc()
        ).first()
        if latest_flag is None or latest_flag.first_flagged_at < now - timedelta(hours=24):
            issues.append({
                "id": "volume_scanner_silent",
                "severity": "warning",
                "category": "data_freshness",
                "title": "Volume scanner running but no tokens flagged in 24h",
                "details": {
                    "latest_flag_at": (
                        latest_flag.first_flagged_at.isoformat()
                        if latest_flag else None
                    ),
                },
                "likely_fix": (
                    "Check DEX Screener / MegaNode connectivity via API Status. "
                    "Could be a genuinely quiet market, but 24h of silence is unusual."
                ),
                "files_to_check": ["backend/scanners/volume_scanner.py"],
                "auto_fix_url": None,
            })

    # Has launch detection surfaced any candidate in 24h?
    lc_recent = db.query(LaunchCandidate).filter(
        LaunchCandidate.detected_at >= now - timedelta(hours=24)
    ).first()
    if lc_recent is None:
        # Only flag this if pair_watcher has been running — otherwise it's a known state
        pw_recent = db.query(_SL).filter(
            _SL.job_name == "pair_watcher",
            _SL.started_at >= now - timedelta(hours=2),
        ).first()
        if pw_recent:
            issues.append({
                "id": "launch_detection_silent",
                "severity": "info",
                "category": "data_freshness",
                "title": "No launch candidates surfaced in 24h (pair_watcher is running)",
                "details": {"note": "Could be legitimate — low-activity market"},
                "likely_fix": "If this persists for days, check GeckoTerminal connectivity.",
                "files_to_check": ["backend/detection/pair_watcher.py"],
                "auto_fix_url": None,
            })

    return issues


def _check_alert_throughput(db: Session) -> list[dict]:
    """Flag the system being too loud on alerts.

    Cap is enforced per UTC calendar day in fire_*_alert. A rolling
    24h window can legitimately span two cap days, so we compare
    against 2× daily cap + small grace — under that is not suspicious.
    """
    from backend.models.alert import Alert

    issues = []
    now = datetime.utcnow()
    since = now - timedelta(hours=24)

    sent_rolling_24h = db.query(Alert).filter(
        Alert.fired_at >= since,
        Alert.telegram_sent.is_(True),
    ).count()

    daily_cap = settings.MAX_ALERTS_PER_DAY + settings.MAX_EXPLOIT_ALERTS_PER_DAY
    # A 24h window spans UP TO two cap-days, so twice the cap is legit.
    two_day_ceiling = daily_cap * 2 + 3  # grace for edge races

    if sent_rolling_24h > two_day_ceiling:
        issues.append({
            "id": "alert_spam",
            "severity": "warning",
            "category": "alerts",
            "title": f"Unusually high alert volume: {sent_rolling_24h} in rolling 24h",
            "details": {
                "sent_rolling_24h": sent_rolling_24h,
                "daily_cap_combined": daily_cap,
                "rolling_24h_ceiling": two_day_ceiling,
                "launch_cap": settings.MAX_ALERTS_PER_DAY,
                "exploit_cap": settings.MAX_EXPLOIT_ALERTS_PER_DAY,
            },
            "likely_fix": "Check alert gating in launch_scorer + exploit_alert.",
            "files_to_check": [
                "backend/detection/launch_scorer.py",
                "backend/alerts/launch_alert.py",
                "backend/alerts/exploit_alert.py",
            ],
            "auto_fix_url": None,
        })

    return issues


@router.get("/health-audit")
def health_audit(db: Session = Depends(get_db)):
    """
    Structured system audit for automated routines.

    Runs scheduler / schema / data-freshness / alert-throughput checks
    and returns a list of issues with severity + remediation hints.
    Pairs with `scripts/routine_health_check.py`.
    """
    issues: list[dict] = []
    issues.extend(_check_scheduler_jobs(db))
    issues.extend(_check_schema_drift(db))
    issues.extend(_check_data_freshness(db))
    issues.extend(_check_alert_throughput(db))

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
# Each of these is idempotent + side-effect-safe. Routines call them
# when an issue's `auto_fix_url` matches.


@router.post("/fix/widen-columns")
def fix_widen_columns():
    """Re-run the _widen_legacy_columns() ALTER migrations.

    Idempotent: Postgres no-ops ALTER TYPE when the target width
    already matches. Safe to call even when nothing is wrong.
    """
    from backend.main import _widen_legacy_columns
    try:
        _widen_legacy_columns()
        return {"status": "ok", "message": "widen_legacy_columns ran"}
    except Exception as e:
        return {"status": "error", "message": str(e)[:300]}


@router.post("/fix/cleanup-orphans")
def fix_cleanup_orphans(db: Session = Depends(get_db)):
    """
    Clean up persistent orphan rows:
      - LaunchCandidate rows with contract_address starting with 'pending:'
        that never resolved to a real deploy (>7 days old).
      - ExploitCandidate rows older than 30 days with no alert fired.
    """
    from backend.models.launch_candidate import LaunchCandidate
    from backend.models.exploit_candidate import ExploitCandidate

    pending_cutoff = datetime.utcnow() - timedelta(days=7)
    exploit_cutoff = datetime.utcnow() - timedelta(days=30)

    pending_deleted = db.query(LaunchCandidate).filter(
        LaunchCandidate.contract_address.like("pending:%"),
        LaunchCandidate.last_signal_at < pending_cutoff,
    ).delete(synchronize_session=False)

    exploit_deleted = db.query(ExploitCandidate).filter(
        ExploitCandidate.detected_at < exploit_cutoff,
        ExploitCandidate.alert_fired.is_(False),
    ).delete(synchronize_session=False)

    db.commit()
    return {
        "status": "ok",
        "pending_launch_candidates_deleted": pending_deleted,
        "old_unalerted_exploits_deleted": exploit_deleted,
    }


@router.get("/nansen-probe")
async def nansen_probe(
    url: str = Query(..., description="Full URL to probe"),
    header: str = Query("apikey", description="Header: apikey | authorization-bearer"),
    method: str = Query("POST", description="HTTP method: POST (default) or GET"),
    body: str = Query(
        '{"chains": ["ethereum"], "pagination": {"page": 1, "per_page": 1}}',
        description="JSON body for POST (default: smart-money/holdings sample)",
    ),
):
    """
    User-driven Nansen probe. Call this with any URL from docs.nansen.ai
    to verify that our NANSEN_API_KEY works against it. Defaults to POST
    with a smart-money/holdings-shaped body since most Nansen endpoints
    are POST.

    Example:
      /api/diagnostics/nansen-probe?url=https://api.nansen.ai/api/v1/smart-money/holdings
    """
    import json as _json

    if not nansen.configured:
        return {"error": "NANSEN_API_KEY is not set"}

    if header.lower() == "authorization-bearer":
        headers = {
            "Authorization": f"Bearer {nansen.key}",
            "accept": "application/json",
            "Content-Type": "application/json",
        }
    else:
        # Docs spec: lowercase apikey
        headers = {
            "apikey": nansen.key,
            "accept": "application/json",
            "Content-Type": "application/json",
        }

    body_json: dict | None = None
    if method.upper() == "POST":
        try:
            body_json = _json.loads(body) if body else {}
        except Exception as e:
            return {"error": f"Invalid JSON body: {e}"}

    r = await _http_probe(
        url, headers=headers, method=method.upper(), json_body=body_json
    )
    return {
        "url_probed": url,
        "method": method.upper(),
        "header_style": header,
        "body_sent": body_json,
        "status": r.get("status"),
        "ok": r.get("ok"),
        "latency_ms": r.get("ms"),
        "error": r.get("error"),
        "body_preview": r.get("preview"),
    }
