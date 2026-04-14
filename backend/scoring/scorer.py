"""
Scoring Engine — Calculates 0-100 pump probability score per token.

Applies weighted signals from volume, supply, holder concentration,
wallet analysis, exchange flows, social, and contract red flags.
Uses tiered scoring where higher tiers REPLACE (not add to) lower tiers.
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import SessionLocal
from backend.models.flagged_token import FlaggedToken
from backend.models.known_wallet import KnownWallet
from backend.models.token_profile import TokenProfile
from backend.models.wallet_activity import WalletActivity
from backend.models.watchlist import Watchlist
from backend.models.token_score_history import TokenScoreHistory

logger = logging.getLogger(__name__)

# How recent a known-operator activity must be to count as "present" for
# scoring. 7 days lines up with the ramp-up window of past confirmed pumps.
KNOWN_OPERATOR_LOOKBACK_DAYS = 7

# Scoring weights — tiered signals use the HIGHER value (not additive)
SCORING_WEIGHTS = {
    # Volume signals (tiered)
    "volume_spike_5x": 15,
    "volume_spike_20x": 25,
    "volume_mcap_ratio_50pct": 10,
    "volume_mcap_ratio_100pct": 15,

    # Supply signals (tiered)
    "float_under_30pct": 15,
    "float_under_20pct": 20,

    # Holder concentration (tiered)
    "top10_over_50pct": 20,
    "top10_over_80pct": 25,

    # Wallet analysis
    "cluster_detected": 25,
    "known_operator_present": 30,

    # Token metadata
    "token_age_under_12mo": 10,
    "trending_narrative": 5,
    "binance_alpha": 10,
    "binance_futures": 5,

    # Exchange flow (trigger signals)
    "exchange_deposit_cluster": 20,
    "exchange_withdrawal_whale": 15,

    # Social signals
    "social_ramp_detected": 5,

    # Contract red flags
    "contract_has_mint": 5,
    "contract_has_pause": 5,
    "contract_has_blacklist": 5,
    "contract_unverified": 10,
}


def has_recent_known_operator_activity(db: Session, contract_address: str) -> bool:
    """
    Returns True if any known operator wallet has a flagged activity for
    this token within the last KNOWN_OPERATOR_LOOKBACK_DAYS days.

    This is the cross-path detection signal: when the volume scanner and
    the wallet tracker independently surface the same token, score it
    higher.
    """
    cutoff = datetime.utcnow() - timedelta(days=KNOWN_OPERATOR_LOOKBACK_DAYS)
    known_addrs_subq = db.query(KnownWallet.wallet_address).filter(
        KnownWallet.is_active.is_(True)
    ).subquery()
    return db.query(WalletActivity.id).filter(
        WalletActivity.token_contract == contract_address.lower(),
        WalletActivity.detected_at >= cutoff,
        WalletActivity.wallet_address.in_(known_addrs_subq),
    ).first() is not None


def calculate_score(
    flagged: FlaggedToken,
    profile: TokenProfile | None,
    watchlist: Watchlist | None,
    known_operator_recent: bool = False,
) -> tuple[int, dict]:
    """
    Calculate pump probability score for a token.
    Returns (normalized_score, score_breakdown).

    IMPORTANT: Tiered signals use the HIGHER value, not additive.
    Example: float_under_20pct (20) replaces float_under_30pct (15).
    """
    breakdown = {}
    raw_score = 0

    # --- Volume signals ---
    if flagged.volume_change_pct:
        change = float(flagged.volume_change_pct)
        if change >= (20 - 1) * 100:  # 20x = 1900% change
            breakdown["volume_spike_20x"] = SCORING_WEIGHTS["volume_spike_20x"]
            raw_score += SCORING_WEIGHTS["volume_spike_20x"]
        elif change >= (5 - 1) * 100:  # 5x = 400% change
            breakdown["volume_spike_5x"] = SCORING_WEIGHTS["volume_spike_5x"]
            raw_score += SCORING_WEIGHTS["volume_spike_5x"]

    if flagged.volume_24h and flagged.market_cap and float(flagged.market_cap) > 0:
        vol_mcap = float(flagged.volume_24h) / float(flagged.market_cap)
        if vol_mcap >= 1.0:
            breakdown["volume_mcap_ratio_100pct"] = SCORING_WEIGHTS["volume_mcap_ratio_100pct"]
            raw_score += SCORING_WEIGHTS["volume_mcap_ratio_100pct"]
        elif vol_mcap >= 0.5:
            breakdown["volume_mcap_ratio_50pct"] = SCORING_WEIGHTS["volume_mcap_ratio_50pct"]
            raw_score += SCORING_WEIGHTS["volume_mcap_ratio_50pct"]

    # --- Profile-based signals ---
    if profile:
        # Float signals (tiered)
        if profile.float_pct is not None:
            flt = float(profile.float_pct)
            if flt < 20:
                breakdown["float_under_20pct"] = SCORING_WEIGHTS["float_under_20pct"]
                raw_score += SCORING_WEIGHTS["float_under_20pct"]
            elif flt < 30:
                breakdown["float_under_30pct"] = SCORING_WEIGHTS["float_under_30pct"]
                raw_score += SCORING_WEIGHTS["float_under_30pct"]

        # Holder concentration (tiered)
        if profile.top_10_holder_pct is not None:
            top10 = float(profile.top_10_holder_pct)
            if top10 > 80:
                breakdown["top10_over_80pct"] = SCORING_WEIGHTS["top10_over_80pct"]
                raw_score += SCORING_WEIGHTS["top10_over_80pct"]
            elif top10 > 50:
                breakdown["top10_over_50pct"] = SCORING_WEIGHTS["top10_over_50pct"]
                raw_score += SCORING_WEIGHTS["top10_over_50pct"]

        # Token metadata
        if profile.token_age_days is not None and profile.token_age_days < 365:
            breakdown["token_age_under_12mo"] = SCORING_WEIGHTS["token_age_under_12mo"]
            raw_score += SCORING_WEIGHTS["token_age_under_12mo"]

        if profile.narrative_tags:
            breakdown["trending_narrative"] = SCORING_WEIGHTS["trending_narrative"]
            raw_score += SCORING_WEIGHTS["trending_narrative"]

        if profile.binance_alpha:
            breakdown["binance_alpha"] = SCORING_WEIGHTS["binance_alpha"]
            raw_score += SCORING_WEIGHTS["binance_alpha"]

        if profile.binance_futures:
            breakdown["binance_futures"] = SCORING_WEIGHTS["binance_futures"]
            raw_score += SCORING_WEIGHTS["binance_futures"]

        # Contract red flags
        if profile.has_mint_function:
            breakdown["contract_has_mint"] = SCORING_WEIGHTS["contract_has_mint"]
            raw_score += SCORING_WEIGHTS["contract_has_mint"]

        if profile.has_pause_function:
            breakdown["contract_has_pause"] = SCORING_WEIGHTS["contract_has_pause"]
            raw_score += SCORING_WEIGHTS["contract_has_pause"]

        if profile.has_blacklist_function:
            breakdown["contract_has_blacklist"] = SCORING_WEIGHTS["contract_has_blacklist"]
            raw_score += SCORING_WEIGHTS["contract_has_blacklist"]

        if profile.is_contract_verified is False:
            breakdown["contract_unverified"] = SCORING_WEIGHTS["contract_unverified"]
            raw_score += SCORING_WEIGHTS["contract_unverified"]

    # --- Watchlist-based signals ---
    if watchlist:
        if watchlist.cluster_detected:
            breakdown["cluster_detected"] = SCORING_WEIGHTS["cluster_detected"]
            raw_score += SCORING_WEIGHTS["cluster_detected"]

        if watchlist.exchange_deposits_detected:
            breakdown["exchange_deposit_cluster"] = SCORING_WEIGHTS["exchange_deposit_cluster"]
            raw_score += SCORING_WEIGHTS["exchange_deposit_cluster"]

        if watchlist.social_signal_detected:
            breakdown["social_ramp_detected"] = SCORING_WEIGHTS["social_ramp_detected"]
            raw_score += SCORING_WEIGHTS["social_ramp_detected"]

    # Known operator present — surface from EITHER:
    #   (a) wallet_analyzer detection (stored in watchlist.score_breakdown), or
    #   (b) recent wallet_activity from a known operator wallet
    operator_from_analyzer = bool(
        watchlist
        and watchlist.score_breakdown
        and watchlist.score_breakdown.get("known_operator_present")
    )
    if operator_from_analyzer or known_operator_recent:
        breakdown["known_operator_present"] = SCORING_WEIGHTS["known_operator_present"]
        raw_score += SCORING_WEIGHTS["known_operator_present"]

    # Normalize to 0-100
    normalized = min(100, raw_score)

    return normalized, breakdown


def determine_confidence(score: int) -> str:
    """Determine confidence level from score."""
    if score >= 70:
        return "HIGH"
    elif score >= 50:
        return "MEDIUM"
    return "LOW"


def determine_status(score: int, current_status: str) -> str:
    """Determine token status based on score."""
    if score >= settings.ALERT_THRESHOLD:
        return "alerted" if current_status != "alerted" else current_status
    elif score >= settings.WATCHLIST_THRESHOLD:
        return "watchlist"
    return current_status  # Keep existing status for low scores


def score_token(db: Session, contract_address: str) -> tuple[int, dict]:
    """Score a single token and update the database."""
    flagged = db.query(FlaggedToken).filter_by(contract_address=contract_address).first()
    if not flagged:
        return 0, {}

    # Don't score tokens with no actual data — they're stubs from auto-flagging
    if not flagged.token_name and not flagged.token_symbol and not flagged.price_usd:
        return 0, {}

    profile = db.query(TokenProfile).filter_by(contract_address=contract_address).first()
    watchlist = db.query(Watchlist).filter_by(contract_address=contract_address).first()
    known_op_recent = has_recent_known_operator_activity(db, contract_address)

    score, breakdown = calculate_score(flagged, profile, watchlist, known_op_recent)
    confidence = determine_confidence(score)
    new_status = determine_status(score, flagged.status)

    # Update flagged token status
    if new_status != flagged.status:
        flagged.status = new_status
        logger.info(
            f"Status change: {flagged.token_symbol} {flagged.status} -> {new_status} (score={score})"
        )

    # Update or create watchlist entry
    if score >= settings.WATCHLIST_THRESHOLD:
        if watchlist:
            watchlist.current_score = score
            watchlist.score_breakdown = breakdown
            watchlist.confidence_level = confidence
            watchlist.last_scored_at = datetime.utcnow()
        else:
            watchlist = Watchlist(
                contract_address=contract_address,
                current_score=score,
                score_breakdown=breakdown,
                confidence_level=confidence,
                added_at=datetime.utcnow(),
                last_scored_at=datetime.utcnow(),
            )
            db.add(watchlist)

    # Record score history
    history = TokenScoreHistory(
        contract_address=contract_address,
        score=score,
        score_breakdown=breakdown,
        recorded_at=datetime.utcnow(),
    )
    db.add(history)

    db.commit()
    return score, breakdown


async def run_score_recalculation():
    """Recalculate scores for all enriched active tokens. Called every 30 minutes.

    Skips stub tokens (no symbol/price) so we don't rack up wasted work or
    create empty watchlist entries — those are filtered at query time.
    """
    logger.info("Starting score recalculation run...")
    db = SessionLocal()
    scored_count = 0
    alert_candidates = []

    try:
        active_tokens = db.query(FlaggedToken).filter(
            FlaggedToken.status.in_(["candidate", "watchlist", "alerted"]),
            FlaggedToken.removed.is_(False),
            FlaggedToken.token_symbol.isnot(None),
            FlaggedToken.price_usd.isnot(None),
        ).all()

        logger.info(f"Recalculating scores for {len(active_tokens)} tokens")

        for token in active_tokens:
            try:
                score, breakdown = score_token(db, token.contract_address)
                scored_count += 1

                if score >= settings.ALERT_THRESHOLD:
                    # Check if alert already fired
                    watchlist = db.query(Watchlist).filter_by(
                        contract_address=token.contract_address
                    ).first()
                    if watchlist and not watchlist.alert_fired:
                        alert_candidates.append({
                            "contract_address": token.contract_address,
                            "symbol": token.token_symbol,
                            "score": score,
                            "breakdown": breakdown,
                        })

            except Exception as e:
                logger.error(f"Error scoring {token.contract_address}: {e}")
                continue

        logger.info(
            f"Score recalculation complete. Scored {scored_count} tokens, "
            f"{len(alert_candidates)} new alert candidates."
        )

        return alert_candidates

    except Exception as e:
        logger.error(f"Score recalculation error: {e}")
        return []
    finally:
        db.close()
