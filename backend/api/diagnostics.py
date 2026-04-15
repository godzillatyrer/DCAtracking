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
from backend.database import get_db
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
    "GeckoTerminal": ["pair_watcher"],
    "Helius": [],
    "Nansen": ["portfolio_gate"],
    "GMGN": [],
    "Telegram": ["wallet_tracker", "scorer", "launch_scorer"],
    "Anthropic": ["scorer", "launch_scorer"],
}


# --- In-process 60s cache -------------------------------------------------

_CACHE: dict[str, tuple[float, dict]] = {}
_TTL = 60.0


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


async def _check_defillama() -> dict:
    ok, ms, _, err = await _timed(defillama._get(f"{defillama.base}/chains"))
    if ok:
        return {"configured": True, "status": "ok", "latency_ms": ms, "detail": "chains list ok"}
    return {"configured": True, "status": "error", "latency_ms": ms, "error": err, "detail": "public API (no key needed)"}


async def _check_geckoterminal() -> dict:
    ok, ms, detail, err = await _timed(geckoterminal._get("/networks"))
    if ok:
        return {"configured": True, "status": "ok", "latency_ms": ms, "detail": "networks list ok"}
    return {"configured": True, "status": "error", "latency_ms": ms, "error": err, "detail": "public API (no key needed)"}


async def _check_helius() -> dict:
    if not helius.configured:
        return {"configured": False, "status": "not_configured", "detail": "HELIUS_API_KEY is empty"}
    ok, ms, detail, err = await _timed(helius.get_slot())
    if ok:
        return {"configured": True, "status": "ok", "latency_ms": ms, "detail": f"slot: {detail}"}
    return {"configured": True, "status": "error", "latency_ms": ms, "error": err}


async def _check_nansen() -> dict:
    if not nansen.configured:
        return {"configured": False, "status": "not_configured", "detail": "NANSEN_API_KEY is empty (optional, paid $150/mo)"}
    # Nansen — cheap label lookup
    ok, ms, _, err = await _timed(nansen.label_address("ethereum", "0x28c6c06298d514db089934071355e5743bf21d60"))
    if ok:
        return {"configured": True, "status": "ok", "latency_ms": ms, "detail": "label lookup ok"}
    return {"configured": True, "status": "error", "latency_ms": ms, "error": err}


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
    ("Helius",         "HELIUS_API_KEY",    "Solana RPC — Solana detection modules (Phase 3, optional)",    _check_helius),
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
