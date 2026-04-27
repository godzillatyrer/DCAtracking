"""Settings + Health + Outcomes + Replay — operator-facing API.

All thresholds are DB-backed and editable from the Settings page —
no env var edits, no redeploys. The Health endpoint bundles scheduler,
API, DB, and alert stats into one payload for the Health dashboard.
The Replay endpoint simulates the dispatcher over historical data
with override thresholds so you can calibrate before shipping.
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from backend import settings_cache
from backend.database import get_db
from backend.models.alert import Alert
from backend.models.alert_outcome import AlertOutcome
from backend.models.cex_address import CexAddress
from backend.models.chain_anomaly import ChainAnomaly
from backend.models.major_coin import MajorCoin
from backend.models.scan_log import ScanLog
from backend.models.solana_known_wallet import SolanaKnownWallet
from backend.models.solana_wallet_activity import SolanaWalletActivity
from backend.models.solana_wallet_stats import SolanaWalletStats

router = APIRouter(prefix="/api", tags=["settings"])


# ─── Settings ────────────────────────────────────────────────────────

@router.get("/settings")
def get_settings():
    return {"items": settings_cache.list_all()}


class SettingUpdate(BaseModel):
    key: str
    value: object


@router.post("/settings")
def update_setting(req: SettingUpdate):
    try:
        return settings_cache.set_value(req.key, req.value)
    except KeyError:
        raise HTTPException(404, detail=f"unknown setting: {req.key}")
    except Exception as e:
        raise HTTPException(400, detail=str(e))


@router.post("/settings/reset")
def reset_setting(req: SettingUpdate):  # reuse the same shape; value is ignored
    out = settings_cache.reset_default(req.key)
    if out is None:
        raise HTTPException(404, detail=f"unknown or no-default setting: {req.key}")
    return out


# ─── CEX addresses (admin) ────────────────────────────────────────────

@router.get("/cex-addresses")
def list_cex_addresses(db: Session = Depends(get_db)):
    rows = db.query(CexAddress).order_by(CexAddress.exchange, CexAddress.name).all()
    return {
        "items": [
            {
                "address": r.address, "name": r.name, "exchange": r.exchange,
                "is_active": r.is_active, "notes": r.notes,
                "added_at": r.added_at.isoformat() if r.added_at else None,
            }
            for r in rows
        ]
    }


class AddCexRequest(BaseModel):
    address: str
    name: str
    exchange: str = ""
    notes: str = ""


@router.post("/cex-addresses")
def add_cex_address(req: AddCexRequest, db: Session = Depends(get_db)):
    if len(req.address) < 32:
        raise HTTPException(400, detail="address looks too short")
    existing = db.query(CexAddress).filter_by(address=req.address).first()
    if existing:
        existing.name = req.name
        existing.exchange = req.exchange or existing.exchange
        existing.notes = req.notes or existing.notes
        existing.is_active = True
    else:
        db.add(CexAddress(
            address=req.address, name=req.name,
            exchange=req.exchange or None, notes=req.notes or None,
        ))
    db.commit()
    return {"ok": True}


@router.post("/cex-addresses/{address}/toggle")
def toggle_cex_address(address: str, db: Session = Depends(get_db)):
    row = db.query(CexAddress).filter_by(address=address).first()
    if not row:
        raise HTTPException(404)
    row.is_active = not row.is_active
    db.commit()
    return {"address": address, "is_active": row.is_active}


# ─── Major-coin admin (anomaly exclusion list) ───────────────────────

@router.get("/major-coins")
def list_major_coins(db: Session = Depends(get_db)):
    rows = db.query(MajorCoin).order_by(MajorCoin.symbol).all()
    return {
        "items": [
            {
                "symbol": r.symbol, "label": r.label,
                "excluded": r.excluded,
                "added_at": r.added_at.isoformat() if r.added_at else None,
            }
            for r in rows
        ]
    }


class AddMajorRequest(BaseModel):
    symbol: str
    label: str = ""


@router.post("/major-coins")
def add_major_coin(req: AddMajorRequest, db: Session = Depends(get_db)):
    sym = (req.symbol or "").strip().upper()
    if not sym:
        raise HTTPException(400, detail="symbol required")
    existing = db.query(MajorCoin).filter_by(symbol=sym).first()
    if existing:
        existing.label = req.label or existing.label
        existing.excluded = True
    else:
        db.add(MajorCoin(symbol=sym, label=req.label or sym, excluded=True))
    db.commit()
    return {"ok": True, "symbol": sym}


@router.post("/major-coins/{symbol}/toggle")
def toggle_major_coin(symbol: str, db: Session = Depends(get_db)):
    row = db.query(MajorCoin).filter_by(symbol=symbol.upper()).first()
    if not row:
        raise HTTPException(404)
    row.excluded = not row.excluded
    db.commit()
    return {"symbol": row.symbol, "excluded": row.excluded}


# ─── Anomaly feed ────────────────────────────────────────────────────

@router.get("/anomalies")
def list_anomalies(
    limit: int = Query(100, ge=1, le=500),
    source: str | None = Query(None),
    event_type: str | None = Query(None),
    only_alerted: bool = Query(False),
    db: Session = Depends(get_db),
):
    q = db.query(ChainAnomaly)
    if source:
        q = q.filter(ChainAnomaly.source == source)
    if event_type:
        q = q.filter(ChainAnomaly.event_type == event_type)
    if only_alerted:
        q = q.filter(ChainAnomaly.is_alerted.is_(True))
    rows = q.order_by(desc(ChainAnomaly.detected_at)).limit(limit).all()
    return {
        "items": [
            {
                "id": r.id,
                "source": r.source,
                "event_type": r.event_type,
                "coin": r.coin,
                "side": r.side,
                "notional_usd": float(r.notional_usd) if r.notional_usd is not None else None,
                "px": float(r.px) if r.px is not None else None,
                "sz": float(r.sz) if r.sz is not None else None,
                "actor_address": r.actor_address,
                "actor_short": (
                    f"{r.actor_address[:6]}…{r.actor_address[-4:]}"
                    if r.actor_address and len(r.actor_address) > 12
                    else r.actor_address
                ),
                "is_fresh_wallet": r.is_fresh_wallet,
                "actor_history_count": r.actor_history_count,
                "tx_hash": r.tx_hash,
                "detected_at": r.detected_at.isoformat() if r.detected_at else None,
                "is_alerted": r.is_alerted,
            }
            for r in rows
        ]
    }


# ─── Outcomes ─────────────────────────────────────────────────────────

@router.get("/outcomes")
def list_outcomes(
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(AlertOutcome, Alert)
        .join(Alert, Alert.id == AlertOutcome.alert_id)
        .order_by(desc(Alert.fired_at))
        .limit(limit)
        .all()
    )
    items = []
    for o, a in rows:
        items.append({
            "alert_id": a.id,
            "fired_at": a.fired_at.isoformat() if a.fired_at else None,
            "alert_type": a.alert_type,
            "mint": o.mint,
            "mint_short": f"{o.mint[:6]}…{o.mint[-4:]}" if o.mint else None,
            "symbol": a.token_symbol,
            "mc_at_alert": float(o.mc_at_alert) if o.mc_at_alert is not None else None,
            "peak_mc_usd": float(o.peak_mc_usd) if o.peak_mc_usd is not None else None,
            "peak_pct": float(o.peak_pct) if o.peak_pct is not None else None,
            "current_mc_usd": float(o.current_mc_usd) if o.current_mc_usd is not None else None,
            "drawdown_pct": float(o.drawdown_pct) if o.drawdown_pct is not None else None,
            "holders_still_holding": o.holders_still_holding,
            "holders_exited": o.holders_exited,
            "holders_summary": o.holders_summary,
            "is_closed": o.is_closed,
            "last_polled_at": o.last_polled_at.isoformat() if o.last_polled_at else None,
        })
    return {"items": items}


@router.get("/outcomes/summary")
def outcome_summary(db: Session = Depends(get_db)):
    """Aggregate stats across closed outcomes — how good are our signals?"""
    rows = (
        db.query(AlertOutcome, Alert)
        .join(Alert, Alert.id == AlertOutcome.alert_id)
        .filter(AlertOutcome.is_closed.is_(True))
        .all()
    )
    by_type: dict[str, dict] = {}
    for o, a in rows:
        t = a.alert_type or "unknown"
        b = by_type.setdefault(t, {
            "count": 0, "avg_peak_pct": 0, "ge_2x": 0, "ge_5x": 0,
            "ge_10x": 0, "losers": 0, "sum_peak_pct": 0,
        })
        b["count"] += 1
        peak = float(o.peak_pct or 0)
        b["sum_peak_pct"] += peak
        if peak >= 100: b["ge_2x"] += 1
        if peak >= 400: b["ge_5x"] += 1
        if peak >= 900: b["ge_10x"] += 1
        if peak < 0: b["losers"] += 1
    summary = []
    for t, b in by_type.items():
        if b["count"]:
            b["avg_peak_pct"] = round(b["sum_peak_pct"] / b["count"], 2)
        b["alert_type"] = t
        b.pop("sum_peak_pct", None)
        summary.append(b)
    return {"summary": summary}


# ─── Health ───────────────────────────────────────────────────────────

@router.get("/health/full")
async def full_health(db: Session = Depends(get_db)):
    """Single-call dashboard health bundle."""
    # Most recent scan log per job
    job_names = [
        "solana_graph_walk",
        "wallet_activity_tracker",
        "wallet_stats_aggregator",
        "alert_outcome_tracker",
        "behavioral_clusterer",
        "hyperliquid_watcher",
        "freshie_dormant_watcher",
        "migration_watcher",
    ]
    jobs = []
    now = datetime.utcnow()
    for name in job_names:
        last = (
            db.query(ScanLog)
            .filter(ScanLog.job_name == name)
            .order_by(desc(ScanLog.started_at))
            .first()
        )
        jobs.append({
            "name": name,
            "last_status": last.status if last else None,
            "last_run": last.started_at.isoformat() if last and last.started_at else None,
            "last_error": (last.error_message or "")[:300] if last and last.status == "error" else None,
            "age_min": (
                int((now - last.started_at).total_seconds() // 60)
                if last and last.started_at else None
            ),
        })

    # DB inventory
    tracked = db.query(func.count(SolanaKnownWallet.wallet_address)).scalar() or 0
    activity = db.query(func.count(SolanaWalletActivity.id)).scalar() or 0
    stats = db.query(func.count(SolanaWalletStats.wallet_address)).scalar() or 0
    snipers = (
        db.query(func.count(SolanaWalletStats.wallet_address))
        .filter(SolanaWalletStats.is_sniper.is_(True))
        .scalar() or 0
    )
    dormant = (
        db.query(func.count(SolanaWalletStats.wallet_address))
        .filter(SolanaWalletStats.is_dormant.is_(True))
        .scalar() or 0
    )
    cex_count = db.query(func.count(CexAddress.address)).scalar() or 0

    # Alert stats (24h / 7d)
    since_24h = now - timedelta(hours=24)
    since_7d = now - timedelta(days=7)
    alerts_24h = db.query(func.count(Alert.id)).filter(Alert.fired_at >= since_24h).scalar() or 0
    alerts_7d = db.query(func.count(Alert.id)).filter(Alert.fired_at >= since_7d).scalar() or 0
    alerts_sent_24h = (
        db.query(func.count(Alert.id))
        .filter(Alert.fired_at >= since_24h, Alert.telegram_sent.is_(True))
        .scalar() or 0
    )

    # External API pings — delegate to existing /api/diagnostics/apis?
    # Return a compact inline list to minimize client round-trips.
    from backend.api.diagnostics import api_status as _api_status  # noqa
    try:
        api_payload = await _api_status(force=0)
    except Exception as e:
        api_payload = {"providers": [], "error": str(e)[:200]}

    return {
        "checked_at": now.isoformat(),
        "jobs": jobs,
        "db": {
            "tracked_wallets": tracked,
            "activity_rows": activity,
            "stats_rows": stats,
            "snipers": snipers,
            "dormant_wallets": dormant,
            "cex_addresses": cex_count,
        },
        "alerts": {
            "fired_24h": alerts_24h,
            "fired_7d": alerts_7d,
            "sent_24h": alerts_sent_24h,
        },
        "apis": api_payload.get("providers", []),
        "settings_count": len(settings_cache.list_all()),
    }


# ─── Replay (backtest) ────────────────────────────────────────────────

class ReplayRequest(BaseModel):
    overrides: dict = {}


@router.post("/settings/replay")
async def replay(req: ReplayRequest):
    """Re-run the dispatcher logic against current activity without
    firing alerts. `overrides` lets you test alternative thresholds
    before pushing them live. Example:
      {"overrides": {"SAME_ENTITY_MIN_WALLETS": 2, "FRESH_MAX_MC_USD": 100000}}
    """
    from backend.detection.alert_dispatcher import run_alert_dispatch
    overrides = dict(req.overrides or {})
    overrides["_replay"] = True
    result = await run_alert_dispatch(overrides)
    return {
        "applied_overrides": {k: v for k, v in overrides.items() if k != "_replay"},
        "result": result,
    }
