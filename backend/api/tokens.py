"""
Token Detail API routes — individual token deep-dive data.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models.flagged_token import FlaggedToken
from backend.models.token_profile import TokenProfile
from backend.models.watchlist import Watchlist
from backend.models.token_score_history import TokenScoreHistory

router = APIRouter(prefix="/api/tokens", tags=["tokens"])


@router.get("/{address}")
def get_token_detail(address: str, db: Session = Depends(get_db)):
    """Full token profile + score breakdown."""
    address = address.lower()
    flagged = db.query(FlaggedToken).filter_by(contract_address=address).first()
    if not flagged:
        raise HTTPException(status_code=404, detail="Token not found")

    profile = db.query(TokenProfile).filter_by(contract_address=address).first()
    watchlist = db.query(Watchlist).filter_by(contract_address=address).first()

    result = {
        "contract_address": address,
        "token_name": flagged.token_name,
        "token_symbol": flagged.token_symbol,
        "chain": flagged.chain,
        "price_usd": str(flagged.price_usd) if flagged.price_usd else None,
        "volume_24h": str(flagged.volume_24h) if flagged.volume_24h else None,
        "volume_change_pct": str(flagged.volume_change_pct) if flagged.volume_change_pct else None,
        "liquidity_usd": str(flagged.liquidity_usd) if flagged.liquidity_usd else None,
        "market_cap": str(flagged.market_cap) if flagged.market_cap else None,
        "fdv": str(flagged.fdv) if flagged.fdv else None,
        "pair_created_at": flagged.pair_created_at.isoformat() if flagged.pair_created_at else None,
        "first_flagged_at": flagged.first_flagged_at.isoformat() if flagged.first_flagged_at else None,
        "status": flagged.status,
        "dex_url": flagged.dex_url,
    }

    if profile:
        result["profile"] = {
            "max_supply": str(profile.max_supply) if profile.max_supply else None,
            "circulating_supply": str(profile.circulating_supply) if profile.circulating_supply else None,
            "float_pct": str(profile.float_pct) if profile.float_pct else None,
            "token_age_days": profile.token_age_days,
            "top_10_holder_pct": str(profile.top_10_holder_pct) if profile.top_10_holder_pct else None,
            "top_1_holder_pct": str(profile.top_1_holder_pct) if profile.top_1_holder_pct else None,
            "deployer_address": profile.deployer_address,
            "is_contract_verified": profile.is_contract_verified,
            "has_proxy": profile.has_proxy,
            "has_mint_function": profile.has_mint_function,
            "has_pause_function": profile.has_pause_function,
            "has_blacklist_function": profile.has_blacklist_function,
            "narrative_tags": profile.narrative_tags,
            "binance_alpha": profile.binance_alpha,
            "binance_futures": profile.binance_futures,
            "exchange_listings": profile.exchange_listings,
        }

    if watchlist:
        result["watchlist"] = {
            "current_score": watchlist.current_score,
            "score_breakdown": watchlist.score_breakdown,
            "confidence_level": watchlist.confidence_level,
            "cluster_detected": watchlist.cluster_detected,
            "cluster_wallet_count": watchlist.cluster_wallet_count,
            "cluster_funding_sources": watchlist.cluster_funding_sources,
            "exchange_deposits_detected": watchlist.exchange_deposits_detected,
            "exchange_deposit_volume": str(watchlist.exchange_deposit_volume) if watchlist.exchange_deposit_volume else None,
            "social_signal_detected": watchlist.social_signal_detected,
            "social_mention_count": watchlist.social_mention_count,
            "ai_briefing": watchlist.ai_briefing,
            "ai_contract_analysis": watchlist.ai_contract_analysis,
            "alert_fired": watchlist.alert_fired,
            "alert_fired_at": watchlist.alert_fired_at.isoformat() if watchlist.alert_fired_at else None,
            "outcome": watchlist.outcome,
            "peak_pct_gain": str(watchlist.peak_pct_gain) if watchlist.peak_pct_gain else None,
        }

    return result


@router.get("/{address}/history")
def get_token_score_history(address: str, db: Session = Depends(get_db)):
    """Score history over time for a token."""
    address = address.lower()
    history = db.query(TokenScoreHistory).filter_by(
        contract_address=address
    ).order_by(TokenScoreHistory.recorded_at.desc()).limit(100).all()

    return [
        {
            "score": h.score,
            "score_breakdown": h.score_breakdown,
            "recorded_at": h.recorded_at.isoformat() if h.recorded_at else None,
        }
        for h in history
    ]


@router.get("/{address}/briefing")
def get_token_briefing(address: str, db: Session = Depends(get_db)):
    """AI-generated briefing for a token."""
    address = address.lower()
    watchlist = db.query(Watchlist).filter_by(contract_address=address).first()
    if not watchlist:
        raise HTTPException(status_code=404, detail="Token not on watchlist")

    return {
        "contract_address": address,
        "ai_briefing": watchlist.ai_briefing,
        "ai_contract_analysis": watchlist.ai_contract_analysis,
    }
