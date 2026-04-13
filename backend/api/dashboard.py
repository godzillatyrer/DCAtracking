"""
Dashboard API routes — summary stats, main scanner views, and activity logs.
"""

import asyncio
from fastapi import APIRouter, Depends, Query, BackgroundTasks
from sqlalchemy import func, case
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models.flagged_token import FlaggedToken
from backend.models.watchlist import Watchlist
from backend.models.alert import Alert
from backend.models.known_wallet import KnownWallet
from backend.models.scan_log import ScanLog

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/overview")
def get_overview(db: Session = Depends(get_db)):
    """Summary stats: active flags, watchlist count, alert count, win rate."""
    active_flags = db.query(func.count(FlaggedToken.id)).filter(
        FlaggedToken.status.in_(["raw", "candidate"]),
        FlaggedToken.removed.is_(False),
    ).scalar()

    watchlist_count = db.query(func.count(Watchlist.id)).scalar()

    alerts_total = db.query(func.count(Alert.id)).scalar()
    alerts_today = db.query(func.count(Alert.id)).filter(
        func.date(Alert.fired_at) == func.current_date()
    ).scalar()

    # Win rate calculation
    reviewed_alerts = db.query(Alert).filter(Alert.reviewed.is_(True)).all()
    wins = sum(1 for a in reviewed_alerts if a.outcome == "pumped")
    win_rate = (wins / len(reviewed_alerts) * 100) if reviewed_alerts else 0

    known_wallets = db.query(func.count(KnownWallet.id)).filter(
        KnownWallet.is_active.is_(True)
    ).scalar()

    return {
        "active_flags": active_flags,
        "watchlist_count": watchlist_count,
        "alerts_total": alerts_total,
        "alerts_today": alerts_today,
        "win_rate": round(win_rate, 1),
        "known_wallets": known_wallets,
    }


@router.get("/flagged")
def get_flagged_tokens(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    status: str | None = None,
    sort_by: str = "last_seen_at",
    sort_dir: str = "desc",
    db: Session = Depends(get_db),
):
    """All flagged tokens with scores, paginated and sortable."""
    query = db.query(FlaggedToken).filter(FlaggedToken.removed.is_(False))

    if status:
        query = query.filter(FlaggedToken.status == status)

    # Sort
    sort_col = getattr(FlaggedToken, sort_by, FlaggedToken.last_seen_at)
    query = query.order_by(sort_col.desc() if sort_dir == "desc" else sort_col.asc())

    total = query.count()
    tokens = query.offset((page - 1) * per_page).limit(per_page).all()

    # Enrich with watchlist scores
    results = []
    for t in tokens:
        watchlist = db.query(Watchlist).filter_by(contract_address=t.contract_address).first()
        results.append({
            "contract_address": t.contract_address,
            "token_name": t.token_name,
            "token_symbol": t.token_symbol,
            "price_usd": str(t.price_usd) if t.price_usd else None,
            "volume_24h": str(t.volume_24h) if t.volume_24h else None,
            "volume_change_pct": str(t.volume_change_pct) if t.volume_change_pct else None,
            "market_cap": str(t.market_cap) if t.market_cap else None,
            "liquidity_usd": str(t.liquidity_usd) if t.liquidity_usd else None,
            "status": t.status,
            "first_flagged_at": t.first_flagged_at.isoformat() if t.first_flagged_at else None,
            "last_seen_at": t.last_seen_at.isoformat() if t.last_seen_at else None,
            "score": watchlist.current_score if watchlist else 0,
            "confidence": watchlist.confidence_level if watchlist else None,
            "dex_url": t.dex_url,
        })

    return {
        "items": results,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
    }


@router.get("/watchlist")
def get_watchlist(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Watchlist tokens with full details."""
    query = db.query(Watchlist).order_by(Watchlist.current_score.desc())
    total = query.count()
    entries = query.offset((page - 1) * per_page).limit(per_page).all()

    results = []
    for w in entries:
        flagged = db.query(FlaggedToken).filter_by(contract_address=w.contract_address).first()
        results.append({
            "contract_address": w.contract_address,
            "token_name": flagged.token_name if flagged else None,
            "token_symbol": flagged.token_symbol if flagged else None,
            "price_usd": str(flagged.price_usd) if flagged and flagged.price_usd else None,
            "volume_24h": str(flagged.volume_24h) if flagged and flagged.volume_24h else None,
            "market_cap": str(flagged.market_cap) if flagged and flagged.market_cap else None,
            "current_score": w.current_score,
            "score_breakdown": w.score_breakdown,
            "confidence_level": w.confidence_level,
            "cluster_detected": w.cluster_detected,
            "cluster_wallet_count": w.cluster_wallet_count,
            "exchange_deposits_detected": w.exchange_deposits_detected,
            "social_signal_detected": w.social_signal_detected,
            "alert_fired": w.alert_fired,
            "alert_fired_at": w.alert_fired_at.isoformat() if w.alert_fired_at else None,
            "outcome": w.outcome,
            "added_at": w.added_at.isoformat() if w.added_at else None,
            "dex_url": flagged.dex_url if flagged else None,
        })

    return {
        "items": results,
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/alerts")
def get_alerts(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Alert history with outcomes."""
    query = db.query(Alert).order_by(Alert.fired_at.desc())
    total = query.count()
    alerts = query.offset((page - 1) * per_page).limit(per_page).all()

    results = []
    for a in alerts:
        results.append({
            "id": a.id,
            "contract_address": a.contract_address,
            "token_symbol": a.token_symbol,
            "score_at_alert": a.score_at_alert,
            "alert_type": a.alert_type,
            "trigger_reason": a.trigger_reason,
            "ai_briefing": a.ai_briefing,
            "price_at_alert": str(a.price_at_alert) if a.price_at_alert else None,
            "market_cap_at_alert": str(a.market_cap_at_alert) if a.market_cap_at_alert else None,
            "telegram_sent": a.telegram_sent,
            "fired_at": a.fired_at.isoformat() if a.fired_at else None,
            "outcome": a.outcome,
            "peak_pct_from_alert": str(a.peak_pct_from_alert) if a.peak_pct_from_alert else None,
            "reviewed": a.reviewed,
        })

    return {
        "items": results,
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/stats")
def get_stats(db: Session = Depends(get_db)):
    """Win rate, avg lead time, accuracy metrics."""
    reviewed = db.query(Alert).filter(Alert.reviewed.is_(True)).all()

    total_reviewed = len(reviewed)
    wins = [a for a in reviewed if a.outcome == "pumped"]
    fizzles = [a for a in reviewed if a.outcome == "fizzled"]

    avg_peak_gain = 0
    avg_lead_time = 0
    if wins:
        peak_gains = [float(a.peak_pct_from_alert) for a in wins if a.peak_pct_from_alert]
        avg_peak_gain = sum(peak_gains) / len(peak_gains) if peak_gains else 0
        lead_times = [a.time_to_peak_hours for a in wins if a.time_to_peak_hours]
        avg_lead_time = sum(lead_times) / len(lead_times) if lead_times else 0

    return {
        "total_alerts": db.query(func.count(Alert.id)).scalar(),
        "total_reviewed": total_reviewed,
        "wins": len(wins),
        "fizzles": len(fizzles),
        "win_rate": round(len(wins) / total_reviewed * 100, 1) if total_reviewed else 0,
        "avg_peak_gain_pct": round(avg_peak_gain, 1),
        "avg_lead_time_hours": round(avg_lead_time, 1),
    }


@router.get("/scan-logs")
def get_scan_logs(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    job_name: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    """Activity log of every scanner run — see exactly what the bot checked."""
    query = db.query(ScanLog)
    if job_name:
        query = query.filter(ScanLog.job_name == job_name)
    if status:
        query = query.filter(ScanLog.status == status)

    total = query.count()
    logs = query.order_by(ScanLog.started_at.desc()).offset(
        (page - 1) * per_page
    ).limit(per_page).all()

    return {
        "items": [
            {
                "id": l.id,
                "job_name": l.job_name,
                "status": l.status,
                "tokens_checked": l.tokens_checked,
                "tokens_flagged": l.tokens_flagged,
                "details": l.details,
                "error_message": l.error_message,
                "started_at": l.started_at.isoformat() if l.started_at else None,
                "finished_at": l.finished_at.isoformat() if l.finished_at else None,
                "duration_seconds": l.duration_seconds,
            }
            for l in logs
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/scan-logs/summary")
def get_scan_log_summary(db: Session = Depends(get_db)):
    """Per-job summary: last run time, last status, total runs, error count."""
    jobs = [
        "volume_scanner", "profile_checker", "wallet_analyzer",
        "exchange_flow", "social_scanner", "wallet_tracker", "scorer", "cleanup",
        "wallet_seeder",
    ]
    summary = []
    for job in jobs:
        total = db.query(func.count(ScanLog.id)).filter(ScanLog.job_name == job).scalar()
        errors = db.query(func.count(ScanLog.id)).filter(
            ScanLog.job_name == job, ScanLog.status == "error"
        ).scalar()
        last = db.query(ScanLog).filter(ScanLog.job_name == job).order_by(
            ScanLog.started_at.desc()
        ).first()

        summary.append({
            "job_name": job,
            "total_runs": total,
            "errors": errors,
            "last_status": last.status if last else "never_run",
            "last_run": last.started_at.isoformat() if last and last.started_at else None,
            "last_details": last.details if last else None,
            "last_duration_seconds": last.duration_seconds if last else None,
        })

    return summary


def _seed_exchange_wallets():
    """Seed exchange hot wallet addresses into the database."""
    from backend.database import SessionLocal
    from backend.models.exchange_wallet import ExchangeWallet

    EXCHANGE_HOT_WALLETS = [
        ("0x8894e0a0c962cb723c1ef8a1b5b5b174d81bd981", "Binance", "hot_wallet"),
        ("0xe2fc31f816a9b94326492132018c3aecc4a93ae1", "Binance", "hot_wallet"),
        ("0xf977814e90da44bfa03b6295a0616a897441acec", "Binance", "hot_wallet"),
        ("0x28c6c06298d514db089934071355e5743bf21d60", "Binance", "hot_wallet"),
        ("0x21a31ee1afc51d94c2efccaa2092ad1028285549", "Binance", "hot_wallet"),
        ("0x0d0707963952f2fba59dd06f2b425ace40b492fe", "Gate.io", "hot_wallet"),
        ("0x1c4b70a3968436b9a0a9cf5205c787eb81bb558c", "Gate.io", "hot_wallet"),
        ("0xf89d7b9c864f589bbf53a82105107622b35eaa40", "Bybit", "hot_wallet"),
        ("0xd6216fc19db775df9774a6e33526131da7d19a2c", "KuCoin", "hot_wallet"),
        ("0x689c56aef474df92d44a1b70850f808488f9769c", "KuCoin", "hot_wallet"),
        ("0x4982085c9e2f89f2ecb8131eca71afad896e89cb", "MEXC", "hot_wallet"),
        ("0x97b9d2aa81164948c17c1beef8e3d5f3f6c1d8c5", "Bitget", "hot_wallet"),
        ("0x6cc5f688a315f3dc28a7781717a9a798a59fda7b", "OKX", "hot_wallet"),
    ]

    db = SessionLocal()
    added = 0
    try:
        for address, name, wallet_type in EXCHANGE_HOT_WALLETS:
            existing = db.query(ExchangeWallet).filter_by(wallet_address=address.lower()).first()
            if not existing:
                wallet = ExchangeWallet(
                    wallet_address=address.lower(),
                    exchange_name=name,
                    wallet_type=wallet_type,
                    chain="bsc",
                    verified=True,
                )
                db.add(wallet)
                added += 1
        db.commit()
    finally:
        db.close()
    return added


def _run_seeder_background():
    """Run the wallet seeder in a background thread."""
    import asyncio
    import logging
    from backend.trackers.wallet_seeder import run_wallet_seeder
    from backend.models.scan_log import ScanLog
    from backend.database import SessionLocal
    from datetime import datetime

    logger = logging.getLogger(__name__)
    started = datetime.utcnow()

    try:
        logger.info("Seeder: Starting exchange wallet seeding...")
        exchange_count = _seed_exchange_wallets()
        logger.info(f"Seeder: Added {exchange_count} exchange wallets")

        logger.info("Seeder: Starting known wallet extraction from confirmed pumps...")
        asyncio.run(run_wallet_seeder())

        finished = datetime.utcnow()
        # Log success
        db = SessionLocal()
        try:
            from backend.models.known_wallet import KnownWallet
            total_wallets = db.query(KnownWallet).count()
            entry = ScanLog(
                job_name="wallet_seeder",
                status="success",
                tokens_checked=5,
                tokens_flagged=total_wallets,
                details=f"Seeded {exchange_count} exchange wallets. Extracted {total_wallets} known wallets from RAVE, SIREN, RIVER, ARIA, STO.",
                started_at=started,
                finished_at=finished,
                duration_seconds=int((finished - started).total_seconds()),
            )
            db.add(entry)
            db.commit()
        finally:
            db.close()
        logger.info(f"Seeder: Complete. {total_wallets} known wallets in database.")

    except Exception as e:
        logger.error(f"Seeder failed: {e}")
        # Log error
        db = SessionLocal()
        try:
            entry = ScanLog(
                job_name="wallet_seeder",
                status="error",
                error_message=str(e),
                started_at=started,
                finished_at=datetime.utcnow(),
            )
            db.add(entry)
            db.commit()
        finally:
            db.close()


@router.post("/seed-wallets")
def trigger_wallet_seed(background_tasks: BackgroundTasks):
    """
    Trigger the wallet seeder to extract known wallets from the 5 confirmed
    pump tokens. Runs in the background — check scan logs for progress.
    """
    background_tasks.add_task(_run_seeder_background)
    return {
        "status": "started",
        "message": "Wallet seeder started in background. This will take 5-10 minutes. Check the Wallet Tracker page for results.",
    }


@router.get("/diagnostics")
async def run_diagnostics():
    """Test API connectivity — checks BscScan, DEX Screener, and database."""
    import httpx
    from backend.config import settings
    from backend.database import SessionLocal
    from backend.models.known_wallet import KnownWallet
    from backend.models.exchange_wallet import ExchangeWallet
    from backend.models.flagged_token import FlaggedToken

    results = {}

    # 1. Check BscScan API
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Test with a simple call — get RAVE deployer
            resp = await client.get(settings.BSCSCAN_BASE_URL, params={
                "module": "contract",
                "action": "getcontractcreation",
                "contractaddresses": "0x17205fab260a7a6383a81452ce6315a39370db97",
                "apikey": settings.BSCSCAN_API_KEY,
            })
            data = resp.json()
            results["bscscan"] = {
                "status": "ok" if data.get("status") == "1" else "error",
                "api_key_set": bool(settings.BSCSCAN_API_KEY),
                "api_key_preview": settings.BSCSCAN_API_KEY[:8] + "..." if settings.BSCSCAN_API_KEY else "NOT SET",
                "response_status": data.get("status"),
                "response_message": data.get("message"),
                "result_preview": str(data.get("result", ""))[:200],
            }
    except Exception as e:
        results["bscscan"] = {"status": "error", "error": str(e)}

    # 2. Check DEX Screener API
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{settings.DEXSCREENER_BASE_URL}/token-boosts/top/v1")
            results["dexscreener"] = {
                "status": "ok" if resp.status_code == 200 else "error",
                "http_code": resp.status_code,
                "tokens_returned": len(resp.json()) if resp.status_code == 200 and isinstance(resp.json(), list) else 0,
            }
    except Exception as e:
        results["dexscreener"] = {"status": "error", "error": str(e)}

    # 3. Check database state
    db = SessionLocal()
    try:
        results["database"] = {
            "status": "ok",
            "known_wallets": db.query(KnownWallet).count(),
            "exchange_wallets": db.query(ExchangeWallet).count(),
            "flagged_tokens": db.query(FlaggedToken).count(),
        }
    except Exception as e:
        results["database"] = {"status": "error", "error": str(e)}
    finally:
        db.close()

    # 4. Config check
    results["config"] = {
        "bscscan_key_set": bool(settings.BSCSCAN_API_KEY),
        "anthropic_key_set": bool(settings.ANTHROPIC_API_KEY),
        "telegram_bot_set": bool(settings.TELEGRAM_BOT_TOKEN),
        "telegram_chat_set": bool(settings.TELEGRAM_CHAT_ID),
    }

    return results
