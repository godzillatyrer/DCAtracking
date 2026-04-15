"""
Module 8: Portfolio-Size Gated Deploy.

Pure filter-tier signal. When a new LaunchCandidate's deployer wallet
has >$1M in portfolio holdings (per Arkham or Nansen), we add a FILTER
tier signal. By design this never triggers alerts on its own — it only
boosts the composite when paired with another signal.

We cache portfolio lookups in `wallet_portfolios` because Arkham/Nansen
rate-limit lookups aggressively.
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from backend.arkham_client import lookup_address
from backend.clients import nansen
from backend.config import settings
from backend.database import SessionLocal
from backend.detection.launch_scorer import record_signal
from backend.models.launch_candidate import LaunchCandidate
from backend.models.launch_signal import TIER_FILTER
from backend.models.wallet_portfolio import WalletPortfolio

logger = logging.getLogger(__name__)

CACHE_FRESHNESS_HOURS = 24


async def _portfolio_usd(db: Session, chain: str, addr: str) -> float | None:
    addr = addr.lower() if addr.startswith("0x") else addr
    cached = db.query(WalletPortfolio).filter_by(
        chain=chain, wallet_address=addr
    ).first()
    if cached and cached.updated_at and cached.updated_at > (
        datetime.utcnow() - timedelta(hours=CACHE_FRESHNESS_HOURS)
    ):
        return float(cached.total_usd or 0)

    total_usd: float | None = None

    # Preferred: Nansen Profiler (richer + more accurate balances).
    # Falls back to Arkham when Nansen isn't configured or returns no
    # data. Nansen's /profiler/address/balances endpoint returns a
    # per-token breakdown; we sum `value_usd` to get the portfolio total.
    if nansen.configured and chain in ("ethereum", "bsc"):
        data = await nansen.profiler_address_balances([addr], chains=[chain])
        if isinstance(data, dict):
            holdings = data.get("data") or data.get("balances") or []
            if isinstance(holdings, list):
                try:
                    total_usd = sum(
                        float(h.get("value_usd") or h.get("valueUsd") or 0)
                        for h in holdings
                    )
                except Exception:
                    total_usd = None

    if total_usd is None:
        try:
            entity = await lookup_address(addr)
            if entity:
                v = entity.get("portfolio", {}).get("totalUsd") or entity.get("totalUsd")
                if v is not None:
                    try:
                        total_usd = float(v)
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"arkham portfolio error: {e}")

    if total_usd is None:
        return None

    row = cached or WalletPortfolio(
        chain=chain, wallet_address=addr, source="nansen" if nansen.configured else "arkham",
    )
    row.total_usd = Decimal(str(round(total_usd, 2)))
    row.updated_at = datetime.utcnow()
    if cached is None:
        db.add(row)
    db.commit()
    return total_usd


async def run_portfolio_gate():
    """Scheduler entry. Scans recent LaunchCandidates for deployer size."""
    logger.info("Starting portfolio_gate run...")
    db = SessionLocal()
    fired = 0
    try:
        cutoff = datetime.utcnow() - timedelta(hours=24)
        candidates = db.query(LaunchCandidate).filter(
            LaunchCandidate.detected_at >= cutoff,
            LaunchCandidate.deployer_address.isnot(None),
            LaunchCandidate.status != "expired",
        ).limit(40).all()
        for cand in candidates:
            addr = cand.deployer_address
            if not addr:
                continue
            usd = await _portfolio_usd(db, cand.chain, addr)
            if usd is None:
                continue
            if usd < settings.PORTFOLIO_GATE_MIN_USD:
                continue
            record_signal(
                db,
                chain=cand.chain,
                contract_address=cand.contract_address,
                signal_type="portfolio_size_gated",
                signal_tier=TIER_FILTER,
                confidence=55,
                evidence={"deployer_portfolio_usd": round(usd, 2)},
                description=f"Deployer holds ${usd:,.0f} in assets.",
            )
            fired += 1
    finally:
        db.close()
    logger.info(f"portfolio_gate complete. {fired} filter signals emitted.")
    return {"signals": fired}
