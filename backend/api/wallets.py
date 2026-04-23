"""
Solana wallet API routes — cabal tracking.

Endpoints:
  POST /api/wallets/solana/extract-from-runner   — paste a runner CA,
        extract its early/profitable wallets, optionally add to DB
  GET  /api/wallets/solana/known                  — list tracked wallets
  GET  /api/wallets/solana/leaderboard            — wallets ranked by
        confidence (lifetime PnL + win rate + freshness)
  GET  /api/wallets/solana/activity               — recent buy/sell feed
  GET  /api/wallets/solana/convergence            — mints bought by N+
        tracked wallets, scored by role × confidence × time-tightness
"""

import asyncio
import math
import time
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, desc
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import get_db
from backend.models.solana_known_wallet import SolanaKnownWallet
from backend.models.solana_wallet_activity import SolanaWalletActivity
from backend.models.solana_wallet_stats import SolanaWalletStats

EXTRACT_TIMEOUT_SECONDS = 90

router = APIRouter(prefix="/api/wallets", tags=["wallets"])


# ─── Short addresses ────────────────────────────────────────────────

def _short(addr: str | None) -> str | None:
    if not addr:
        return None
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 12 else addr


# ─── DexScreener mint info cache (liquidity / MC) ───────────────────

_DEX_CACHE: dict[str, tuple[float, dict]] = {}
_DEX_TTL = 120.0


async def _dexscreener_info(mint: str) -> dict | None:
    entry = _DEX_CACHE.get(mint)
    if entry and time.time() - entry[0] < _DEX_TTL:
        return entry[1]
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                f"{settings.DEXSCREENER_BASE_URL}/latest/dex/tokens/{mint}"
            )
            if resp.status_code != 200:
                return None
            pairs = (resp.json() or {}).get("pairs") or []
            if not pairs:
                return None
            # Best pair by USD liquidity
            best = max(
                pairs,
                key=lambda p: float(((p.get("liquidity") or {}).get("usd")) or 0),
            )
            info = {
                "price_usd": float(best.get("priceUsd") or 0),
                "liquidity_usd": float(((best.get("liquidity") or {}).get("usd")) or 0),
                "market_cap_usd": float(best.get("marketCap") or best.get("fdv") or 0),
                "volume_24h_usd": float(((best.get("volume") or {}).get("h24")) or 0),
                "pair_url": best.get("url"),
                "symbol": (best.get("baseToken") or {}).get("symbol"),
                "name": (best.get("baseToken") or {}).get("name"),
            }
            _DEX_CACHE[mint] = (time.time(), info)
            return info
    except Exception:
        return None


# ─── Extract from runner ─────────────────────────────────────────────

class ExtractFromRunnerRequest(BaseModel):
    mint: str
    symbol: str = ""
    min_profit_usd: float = 500.0
    min_profit_mult: float = 0.0
    max_wallets: int = 60
    auto_add: bool = True


@router.post("/solana/extract-from-runner")
async def extract_from_runner(
    req: ExtractFromRunnerRequest,
    db: Session = Depends(get_db),
):
    from backend.detection.cabal_extractor import (
        extract_cabal_wallets,
        add_cabal_wallets_to_db,
    )
    try:
        wallets, debug = await asyncio.wait_for(
            extract_cabal_wallets(
                req.mint,
                min_profit_usd=req.min_profit_usd,
                min_profit_mult=req.min_profit_mult,
                max_wallets=req.max_wallets,
            ),
            timeout=EXTRACT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=f"Extraction timed out after {EXTRACT_TIMEOUT_SECONDS}s.",
        )
    added = 0
    if req.auto_add and wallets:
        added = add_cabal_wallets_to_db(db, wallets, req.mint, req.symbol)
    return {
        "mint": req.mint,
        "symbol": req.symbol,
        "wallets_found": len(wallets),
        "wallets_added": added,
        "wallets": [
            {
                "address": w["address"],
                "total_profit_usd": w["total_profit_usd"],
                "profit_multiplier": w["profit_multiplier"],
                "cost_usd": w["cost_usd"],
                "balance_usd": w["balance_usd"],
                "pct_of_supply": w["pct_of_supply"],
                "is_smart_money": w["is_smart_money"],
                "still_holding": w["still_holding"],
                "fully_exited": w["fully_exited"],
                "tags": w["tags"],
                "sources": w["sources"],
            }
            for w in wallets
        ],
        "debug": debug,
    }


# ─── Tracked wallets list ────────────────────────────────────────────

@router.get("/solana/known")
def get_solana_known_wallets(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    role: str | None = None,
    db: Session = Depends(get_db),
):
    query = db.query(SolanaKnownWallet).filter(
        SolanaKnownWallet.is_active.is_(True)
    )
    if role:
        query = query.filter(SolanaKnownWallet.role == role)
    total = query.count()
    wallets = query.order_by(SolanaKnownWallet.added_at.desc()).offset(
        (page - 1) * per_page
    ).limit(per_page).all()

    # Join in stats so the UI can show confidence + entity info inline
    addrs = [w.wallet_address for w in wallets]
    stats_by_addr = {
        s.wallet_address: s for s in (
            db.query(SolanaWalletStats)
            .filter(SolanaWalletStats.wallet_address.in_(addrs))
            .all()
        )
    }

    items = []
    for w in wallets:
        s = stats_by_addr.get(w.wallet_address)
        items.append({
            "wallet_address": w.wallet_address,
            "wallet_short": _short(w.wallet_address),
            "label": w.label,
            "role": w.role,
            "associated_token": w.associated_token,
            "associated_mint": w.associated_mint,
            "associated_mint_short": _short(w.associated_mint),
            "funding_source": w.funding_source,
            "funding_source_short": _short(w.funding_source),
            "total_profit_est": str(w.total_profit_est) if w.total_profit_est else None,
            "added_at": w.added_at.isoformat() if w.added_at else None,
            "notes": w.notes,
            "confidence_score": float(s.confidence_score) if s and s.confidence_score is not None else None,
            "net_profit_usd": float(s.net_profit_usd) if s and s.net_profit_usd is not None else None,
            "win_count": (s.win_count if s else None),
            "loss_count": (s.loss_count if s else None),
            "mints_traded": (s.mints_traded if s else None),
            "entity_id": (s.entity_id if s else None),
            "entity_size": (s.entity_size if s else None),
            "last_activity_at": s.last_activity_at.isoformat() if s and s.last_activity_at else None,
        })
    return {"items": items, "total": total, "page": page, "per_page": per_page}


# ─── Leaderboard ──────────────────────────────────────────────────────

@router.get("/solana/leaderboard")
def get_leaderboard(
    limit: int = Query(50, ge=1, le=200),
    min_confidence: float = Query(0.0, ge=0.0),
    db: Session = Depends(get_db),
):
    """Top wallets by composite confidence score."""
    rows = (
        db.query(SolanaWalletStats, SolanaKnownWallet)
        .outerjoin(
            SolanaKnownWallet,
            SolanaKnownWallet.wallet_address == SolanaWalletStats.wallet_address,
        )
        .filter(SolanaWalletStats.confidence_score >= min_confidence)
        .order_by(desc(SolanaWalletStats.confidence_score))
        .limit(limit)
        .all()
    )
    items = []
    for s, w in rows:
        closed = (s.win_count or 0) + (s.loss_count or 0)
        items.append({
            "wallet_address": s.wallet_address,
            "wallet_short": _short(s.wallet_address),
            "role": (w.role if w else None),
            "label": (w.label if w else None),
            "confidence_score": float(s.confidence_score) if s.confidence_score is not None else 0.0,
            "net_profit_usd": float(s.net_profit_usd) if s.net_profit_usd is not None else 0.0,
            "total_buy_usd": float(s.total_buy_usd) if s.total_buy_usd is not None else 0.0,
            "total_sell_usd": float(s.total_sell_usd) if s.total_sell_usd is not None else 0.0,
            "win_count": s.win_count or 0,
            "loss_count": s.loss_count or 0,
            "win_rate": round((s.win_count or 0) / closed, 3) if closed else None,
            "mints_traded": s.mints_traded or 0,
            "mints_closed": s.mints_closed or 0,
            "best_mint": s.best_mint,
            "best_mint_short": _short(s.best_mint),
            "best_mint_profit_usd": float(s.best_mint_profit_usd) if s.best_mint_profit_usd is not None else None,
            "last_activity_at": s.last_activity_at.isoformat() if s.last_activity_at else None,
            "entity_id": s.entity_id,
            "entity_size": s.entity_size or 1,
        })
    return {"items": items}


# ─── Activity feed ───────────────────────────────────────────────────

@router.get("/solana/activity")
def get_solana_activity(
    limit: int = Query(50, ge=1, le=200),
    since_minutes: int = Query(60 * 24, ge=1, le=60 * 24 * 7),
    only_buys: bool = True,
    db: Session = Depends(get_db),
):
    since = datetime.utcnow() - timedelta(minutes=since_minutes)
    query = (
        db.query(SolanaWalletActivity, SolanaKnownWallet, SolanaWalletStats)
        .outerjoin(
            SolanaKnownWallet,
            SolanaKnownWallet.wallet_address == SolanaWalletActivity.wallet_address,
        )
        .outerjoin(
            SolanaWalletStats,
            SolanaWalletStats.wallet_address == SolanaWalletActivity.wallet_address,
        )
        .filter(SolanaWalletActivity.detected_at >= since)
    )
    if only_buys:
        query = query.filter(SolanaWalletActivity.activity_type == "spl_buy")

    rows = query.order_by(desc(SolanaWalletActivity.detected_at)).limit(limit).all()

    return {
        "since": since.isoformat(),
        "items": [
            {
                "wallet_address": a.wallet_address,
                "wallet_short": _short(a.wallet_address),
                "wallet_role": (w.role if w else None),
                "wallet_confidence": float(s.confidence_score) if s and s.confidence_score is not None else None,
                "activity_type": a.activity_type,
                "token_mint": a.token_mint,
                "token_mint_short": _short(a.token_mint),
                "token_symbol": a.token_symbol,
                "amount": str(a.amount) if a.amount else None,
                "value_usd": str(a.value_usd) if a.value_usd else None,
                "signature": a.signature,
                "detected_at": a.detected_at.isoformat() if a.detected_at else None,
                "is_new_token": a.is_new_token,
            }
            for (a, w, s) in rows
        ],
    }


# ─── Convergence ─────────────────────────────────────────────────────

_ROLE_WEIGHT = {
    "cabal_trader": 1.0,
    "cabal_linked": 0.7,
    "cabal_linked_2": 0.5,
}


def _wallet_weight(role: str | None, confidence: float | None,
                   entity_size: int | None) -> float:
    """Per-wallet contribution to a cluster score. Role is the floor;
    confidence amplifies; shared funder (large entity) divides so
    sockpuppets don't stack."""
    role_w = _ROLE_WEIGHT.get(role or "", 0.5)
    conf_bump = 1.0 + min(float(confidence or 0), 10.0) / 10.0
    sybil_div = math.sqrt(max(entity_size or 1, 1))
    return round(role_w * conf_bump / sybil_div, 3)


def _time_tightness(timestamps: list[datetime]) -> float:
    """1.0 if all same minute, decays as spread widens."""
    real = [t for t in timestamps if t]
    if len(real) < 2:
        return 1.0
    span_min = (max(real) - min(real)).total_seconds() / 60.0
    return round(1.0 / (1.0 + span_min / 10.0), 3)


@router.get("/solana/convergence")
async def get_solana_convergence(
    window_minutes: int = Query(60, ge=5, le=60 * 24),
    min_wallets: int = Query(2, ge=2, le=20),
    include_market: bool = Query(True),
    db: Session = Depends(get_db),
):
    """Mints bought by N+ tracked wallets in the window. Score factors:
      - per-wallet weight = role × (1 + confidence/10) / sqrt(entity_size)
      - time-tightness multiplier (tight clusters > spread ones)
      - optional: live liquidity/market cap from DexScreener

    Shared-funder wallets get divided down so five sockpuppets funded
    by the same operator ≈ one signal.
    """
    since = datetime.utcnow() - timedelta(minutes=window_minutes)

    per_wallet = (
        db.query(
            SolanaWalletActivity.token_mint.label("mint"),
            SolanaWalletActivity.wallet_address.label("wallet"),
            func.min(SolanaWalletActivity.detected_at).label("first_buy"),
            func.max(SolanaWalletActivity.detected_at).label("last_buy"),
            func.sum(SolanaWalletActivity.value_usd).label("value_usd"),
            func.count(SolanaWalletActivity.id).label("tx_count"),
        )
        .filter(
            SolanaWalletActivity.detected_at >= since,
            SolanaWalletActivity.activity_type == "spl_buy",
            SolanaWalletActivity.token_mint.isnot(None),
        )
        .group_by(
            SolanaWalletActivity.token_mint,
            SolanaWalletActivity.wallet_address,
        )
        .subquery()
    )

    rows = (
        db.query(
            per_wallet.c.mint,
            per_wallet.c.wallet,
            per_wallet.c.first_buy,
            per_wallet.c.last_buy,
            per_wallet.c.value_usd,
            per_wallet.c.tx_count,
            SolanaKnownWallet.role,
            SolanaWalletStats.confidence_score,
            SolanaWalletStats.entity_id,
            SolanaWalletStats.entity_size,
            SolanaWalletStats.win_count,
            SolanaWalletStats.loss_count,
        )
        .outerjoin(
            SolanaKnownWallet,
            SolanaKnownWallet.wallet_address == per_wallet.c.wallet,
        )
        .outerjoin(
            SolanaWalletStats,
            SolanaWalletStats.wallet_address == per_wallet.c.wallet,
        )
        .all()
    )

    clusters: dict[str, dict] = {}
    for (mint, wallet, first_buy, last_buy, value_usd, tx_count, role,
         conf, entity_id, entity_size, wins, losses) in rows:
        c = clusters.setdefault(mint, {
            "mint": mint,
            "mint_short": _short(mint),
            "wallets": [],
            "entity_ids": set(),
            "first_buy": first_buy,
            "last_buy": last_buy,
            "total_value_usd": 0.0,
        })
        weight = _wallet_weight(role, conf, entity_size)
        closed = (wins or 0) + (losses or 0)
        win_rate = (wins or 0) / closed if closed else None
        c["wallets"].append({
            "wallet_address": wallet,
            "wallet_short": _short(wallet),
            "role": role,
            "confidence_score": float(conf) if conf is not None else 0.0,
            "win_rate": round(win_rate, 3) if win_rate is not None else None,
            "entity_id": entity_id,
            "entity_size": entity_size or 1,
            "first_buy": first_buy.isoformat() if first_buy else None,
            "last_buy": last_buy.isoformat() if last_buy else None,
            "value_usd": float(value_usd) if value_usd else 0.0,
            "tx_count": tx_count,
            "weight": weight,
        })
        if entity_id is not None:
            c["entity_ids"].add(entity_id)
        if first_buy and (c["first_buy"] is None or first_buy < c["first_buy"]):
            c["first_buy"] = first_buy
        if last_buy and (c["last_buy"] is None or last_buy > c["last_buy"]):
            c["last_buy"] = last_buy
        c["total_value_usd"] += float(value_usd) if value_usd else 0.0

    results = []
    for c in clusters.values():
        if len(c["wallets"]) < min_wallets:
            continue
        tightness = _time_tightness([
            datetime.fromisoformat(w["last_buy"]) if w["last_buy"] else None
            for w in c["wallets"]
        ])
        raw_score = sum(w["weight"] for w in c["wallets"])
        score = round(raw_score * (1.0 + tightness) / 2.0, 2)
        c["wallet_count"] = len(c["wallets"])
        c["distinct_entities"] = len(c["entity_ids"]) or c["wallet_count"]
        c["first_buy"] = c["first_buy"].isoformat() if c["first_buy"] else None
        c["last_buy"] = c["last_buy"].isoformat() if c["last_buy"] else None
        c["total_value_usd"] = round(c["total_value_usd"], 2)
        c["time_tightness"] = tightness
        c["score"] = score
        c.pop("entity_ids", None)
        results.append(c)

    results.sort(key=lambda r: (-r["score"], -r["wallet_count"]))

    # Enrich the top clusters with liquidity / MC in parallel. We only
    # do this for the results the user is likely to act on, to keep
    # response time tight.
    if include_market and results:
        top = results[:15]
        mkt = await asyncio.gather(
            *[_dexscreener_info(c["mint"]) for c in top],
            return_exceptions=True,
        )
        for c, m in zip(top, mkt):
            if isinstance(m, Exception) or not m:
                c["market"] = None
            else:
                c["market"] = m
                if m.get("symbol"):
                    c["symbol"] = m["symbol"]

    return {
        "window_minutes": window_minutes,
        "min_wallets": min_wallets,
        "since": since.isoformat(),
        "clusters": results,
    }
