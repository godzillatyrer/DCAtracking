"""
Step 3: Wallet Analyzer — Wallet cluster detection.

Analyzes top holders of candidate tokens to detect coordinated wallet clusters
by tracing shared funding sources and synchronized buy patterns.

Each analysis is expensive (~50 RPC chains for top holders + per-holder
funding traces). To avoid the job exceeding its 4-hour interval and
silently never completing, we cap each run to ANALYZE_BATCH_SIZE tokens
and skip tokens that were analyzed in the last ANALYZE_TTL_HOURS.
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.bscscan_client import bscscan_request
from backend.models.flagged_token import FlaggedToken
from backend.models.known_wallet import KnownWallet
from backend.models.watchlist import Watchlist

logger = logging.getLogger(__name__)

# Cluster detection thresholds
MIN_SHARED_FUNDING_FOR_CLUSTER = 3  # If 3+ holders share a funding source
FRESH_WALLET_MAX_AGE_DAYS = 30

# Batching
ANALYZE_BATCH_SIZE = 20         # max tokens per run
ANALYZE_TTL_HOURS = 12          # don't re-analyze a token within this window


async def get_token_holders_extended(contract_address: str) -> list[dict]:
    """Get top 50 token holders."""
    holders = []
    for page in range(1, 6):  # 5 pages x 10 = 50 holders
        data = await bscscan_request({
            "module": "token",
            "action": "tokenholderlist",
            "contractaddress": contract_address,
            "page": str(page),
            "offset": "10",
        })
        if data and isinstance(data.get("result"), list):
            holders.extend(data["result"])
        else:
            break
    return holders


async def get_first_transactions(wallet_address: str, count: int = 5) -> list[dict]:
    """Get the first N transactions for a wallet (sorted ascending)."""
    data = await bscscan_request({
        "module": "account",
        "action": "txlist",
        "address": wallet_address,
        "startblock": "0",
        "endblock": "99999999",
        "page": "1",
        "offset": str(count),
        "sort": "asc",
    })
    if data and isinstance(data.get("result"), list):
        return data["result"]
    return []


async def get_wallet_token_transfers(
    wallet_address: str, contract_address: str
) -> list[dict]:
    """Get token transfers for a specific wallet and token."""
    data = await bscscan_request({
        "module": "account",
        "action": "tokentx",
        "address": wallet_address,
        "contractaddress": contract_address,
        "page": "1",
        "offset": "50",
        "sort": "asc",
    })
    if data and isinstance(data.get("result"), list):
        return data["result"]
    return []


def find_funding_source(transactions: list[dict], wallet_address: str) -> str | None:
    """
    Find the original gas funding address for a wallet.
    The first incoming BNB transaction reveals who funded this wallet.
    """
    wallet_lower = wallet_address.lower()
    for tx in transactions:
        if tx.get("to", "").lower() == wallet_lower and float(tx.get("value", 0)) > 0:
            return tx.get("from", "").lower()
    return None


def calculate_wallet_age_days(transactions: list[dict]) -> int | None:
    """Calculate wallet age from first transaction."""
    if not transactions:
        return None
    try:
        first_ts = int(transactions[0].get("timeStamp", 0))
        first_date = datetime.utcfromtimestamp(first_ts)
        return (datetime.utcnow() - first_date).days
    except (ValueError, TypeError):
        return None


async def analyze_holders(
    contract_address: str, db: Session
) -> dict:
    """
    Analyze top holders for cluster patterns.
    Returns cluster analysis results.
    """
    result = {
        "cluster_detected": False,
        "cluster_wallet_count": 0,
        "cluster_funding_sources": [],
        "known_operator_present": False,
        "known_operator_wallets": [],
        "fresh_wallet_count": 0,
        "holder_details": [],
    }

    # Get top holders
    holders = await get_token_holders_extended(contract_address)
    if not holders:
        logger.warning(f"No holders found for {contract_address}")
        return result

    # Track funding sources for cluster detection
    funding_map: dict[str, list[str]] = defaultdict(list)
    known_wallets_in_db = {
        w.wallet_address.lower()
        for w in db.query(KnownWallet).filter(KnownWallet.is_active.is_(True)).all()
    }

    for holder in holders:
        holder_address = holder.get("TokenHolderAddress", "").lower()
        if not holder_address:
            continue

        # Check if this is a known operator wallet
        if holder_address in known_wallets_in_db:
            result["known_operator_present"] = True
            result["known_operator_wallets"].append(holder_address)

        # Get first transactions to trace funding
        first_txs = await get_first_transactions(holder_address, count=5)
        funding_source = find_funding_source(first_txs, holder_address)
        wallet_age = calculate_wallet_age_days(first_txs)

        if wallet_age is not None and wallet_age < FRESH_WALLET_MAX_AGE_DAYS:
            result["fresh_wallet_count"] += 1

        if funding_source:
            funding_map[funding_source].append(holder_address)

        result["holder_details"].append({
            "address": holder_address,
            "balance": holder.get("TokenHolderQuantity", "0"),
            "funding_source": funding_source,
            "wallet_age_days": wallet_age,
            "is_known_operator": holder_address in known_wallets_in_db,
        })

    # Detect clusters: multiple holders sharing the same funding source
    for funder, funded_wallets in funding_map.items():
        if len(funded_wallets) >= MIN_SHARED_FUNDING_FOR_CLUSTER:
            result["cluster_detected"] = True
            result["cluster_wallet_count"] += len(funded_wallets)
            result["cluster_funding_sources"].append(funder)

    return result


def update_watchlist_entry(db: Session, contract_address: str, analysis: dict):
    """Create or update a watchlist entry with analysis results.

    Stores `known_operator_present` and `known_operator_wallets` inside
    the watchlist's score_breakdown JSONB so the scorer can pick them up
    without needing a schema change.
    """
    existing = db.query(Watchlist).filter_by(contract_address=contract_address).first()

    watchlist_data = {
        "cluster_detected": analysis["cluster_detected"],
        "cluster_wallet_count": analysis["cluster_wallet_count"],
        "cluster_funding_sources": analysis["cluster_funding_sources"],
        "last_scored_at": datetime.utcnow(),
    }

    if existing:
        for key, value in watchlist_data.items():
            setattr(existing, key, value)
        # Merge known-operator info into score_breakdown so scorer reads it later
        breakdown = dict(existing.score_breakdown or {})
        breakdown["known_operator_present"] = bool(analysis.get("known_operator_present"))
        breakdown["known_operator_wallets"] = analysis.get("known_operator_wallets") or []
        existing.score_breakdown = breakdown
    else:
        entry = Watchlist(
            contract_address=contract_address,
            score_breakdown={
                "known_operator_present": bool(analysis.get("known_operator_present")),
                "known_operator_wallets": analysis.get("known_operator_wallets") or [],
            },
            **watchlist_data,
        )
        db.add(entry)

    # We deliberately do NOT auto-promote to "watchlist" status here. The
    # scorer is the single source of truth for watchlist promotion based on
    # the combined score (see scorer.determine_status).

    db.commit()


def _select_tokens_to_analyze(db: Session, batch_size: int) -> list[FlaggedToken]:
    """
    Pick up to `batch_size` tokens to analyze, prioritising:
      1. Tokens with real market data (skip stubs from auto-flagging)
      2. Tokens that have never been analyzed
      3. Tokens whose last analysis is older than ANALYZE_TTL_HOURS
    Sorted by last_seen_at desc so we focus on currently-active tokens.
    """
    cutoff = datetime.utcnow() - timedelta(hours=ANALYZE_TTL_HOURS)

    # Tokens are eligible for analysis once profile_checker has promoted them.
    eligible = (
        db.query(FlaggedToken)
        .outerjoin(Watchlist, Watchlist.contract_address == FlaggedToken.contract_address)
        .filter(
            FlaggedToken.status.in_(["candidate", "watchlist"]),
            FlaggedToken.removed.is_(False),
            FlaggedToken.token_symbol.isnot(None),
            FlaggedToken.price_usd.isnot(None),
            # never analyzed (no watchlist row) OR analyzed long enough ago
            (Watchlist.last_scored_at.is_(None)) | (Watchlist.last_scored_at < cutoff),
        )
        .order_by(FlaggedToken.last_seen_at.desc())
        .limit(batch_size)
        .all()
    )
    return eligible


async def run_wallet_analyzer():
    """Main wallet analyzer entry point. Called every 4 hours."""
    logger.info("Starting wallet analyzer run...")
    db = SessionLocal()
    analyzed_count = 0
    cluster_count = 0
    known_operator_count = 0

    try:
        candidates = _select_tokens_to_analyze(db, ANALYZE_BATCH_SIZE)
        total_eligible = (
            db.query(FlaggedToken)
            .filter(
                FlaggedToken.status.in_(["candidate", "watchlist"]),
                FlaggedToken.removed.is_(False),
                FlaggedToken.token_symbol.isnot(None),
                FlaggedToken.price_usd.isnot(None),
            )
            .count()
        )
        logger.info(
            f"Wallet analyzer: {total_eligible} eligible tokens, "
            f"processing {len(candidates)} this run"
        )

        for token in candidates:
            try:
                analysis = await analyze_holders(token.contract_address, db)
                update_watchlist_entry(db, token.contract_address, analysis)
                analyzed_count += 1

                if analysis["cluster_detected"]:
                    cluster_count += 1
                    logger.warning(
                        f"CLUSTER DETECTED: {token.token_symbol} — "
                        f"{analysis['cluster_wallet_count']} wallets, "
                        f"{len(analysis['cluster_funding_sources'])} funding sources"
                    )
                if analysis["known_operator_present"]:
                    known_operator_count += 1
                    logger.warning(
                        f"KNOWN OPERATOR: {token.token_symbol} — "
                        f"matches: {analysis['known_operator_wallets']}"
                    )

            except Exception as e:
                logger.error(f"Error analyzing {token.contract_address}: {e}")
                continue

        logger.info(
            f"Wallet analyzer complete. Analyzed {analyzed_count} tokens, "
            f"{cluster_count} with clusters, {known_operator_count} with known operators."
        )

    except Exception as e:
        logger.error(f"Wallet analyzer error: {e}")
    finally:
        db.close()
