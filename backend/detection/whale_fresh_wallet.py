"""
Module 3: Whale-Funds-Fresh-Wallet → Deploy  (the TRUMP pattern).

Two-phase detection:

  Phase A (funding watcher, every 10 min):
    Walk recent outgoing txs from every KnownWallet with a "whale /
    treasury / vc / founder / operator" role. Any outgoing tx to a
    FRESH recipient (nonce ≤ N, age ≤ M days) with USD value above
    WHALE_FUNDING_MIN_USD is recorded as a WalletFundingEdge.

  Phase B (deploy matcher, reused from deployer_watcher):
    Every new contract deployment is cross-referenced against the
    WalletFundingEdge table. If the deployer address was funded by a
    watched whale within WHALE_FRESH_DEPLOY_WINDOW_HOURS, we fire a
    SELF_S-tier signal (single-signal S-tier alert).

This is the rarest + highest-confidence signal we have.
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from backend.bscscan_client import _hex_to_int, _rpc_call
from backend.clients import defillama
from backend.config import settings
from backend.database import SessionLocal
from backend.detection.launch_scorer import record_signal
from backend.models.known_wallet import KnownWallet
from backend.models.wallet_funding_graph import WalletFundingEdge
from backend.models.launch_signal import TIER_SELF_S

logger = logging.getLogger(__name__)


# Which roles count as "whales" whose funding out we watch.
WHALE_ROLES = {
    "whale", "treasury", "vc", "founder", "operator",
    "golden_deployer", "distributor",
}

# Max whales scanned per run. With 500+ KnownWallet rows in operator/
# distributor roles, scanning all of them per 10-min interval routinely
# exceeded the run budget (same root cause as the wallet_tracker stale
# bug). ORDER BY random() + LIMIT N rotates the full set over time.
MAX_WHALES_PER_RUN = 40


async def _get_nonce(address: str) -> int:
    result = await _rpc_call("eth_getTransactionCount", [address.lower(), "latest"])
    return _hex_to_int(result) if result else 0


async def _bnb_price_usd() -> float:
    prices = await defillama.current_prices(["coingecko:binancecoin"])
    entry = prices.get("coingecko:binancecoin") or {}
    return float(entry.get("price") or 0.0)


async def _fresh_wallet(address: str) -> tuple[bool, int]:
    """True if recipient looks fresh (nonce <= threshold)."""
    nonce = await _get_nonce(address)
    return nonce <= settings.FRESH_WALLET_MAX_NONCE, nonce


async def _recent_outgoing_txs(wallet: KnownWallet, since_block: int) -> list[dict]:
    """
    Get recent outgoing token + native transfers for a wallet.
    We reuse the wallet_tracker's existing data pull via MegaNode.
    """
    from backend.bscscan_client import _get_txlist
    data = await _get_txlist(wallet.wallet_address, page=1, offset=50, sort="desc")
    if not data or not isinstance(data.get("result"), list):
        return []
    return [t for t in data["result"] if int(t.get("blockNumber", 0) or 0) >= since_block]


async def record_funding_edges():
    """Phase A — scan whale outgoing txs, record funding edges.

    Batched like wallet_tracker: with 500+ known wallets, iterating all
    of them per 10-min run routinely exceeded the interval budget. We
    random-sample MAX_WHALES_PER_RUN per run; over enough runs we cover
    the full set. This stops the job from going stale + OOMing.
    """
    import gc
    from sqlalchemy import func as _sql_func

    logger.info("Starting whale funding scanner...")
    db = SessionLocal()
    bnb_price = 0.0
    try:
        bnb_price = await _bnb_price_usd()

        total_whales = db.query(KnownWallet).filter(
            KnownWallet.is_active.is_(True),
            KnownWallet.role.in_(list(WHALE_ROLES)),
        ).count()

        whales = (
            db.query(KnownWallet)
            .filter(
                KnownWallet.is_active.is_(True),
                KnownWallet.role.in_(list(WHALE_ROLES)),
            )
            .order_by(_sql_func.random())
            .limit(MAX_WHALES_PER_RUN)
            .all()
        )

        logger.info(
            f"Whale funding scanner: {len(whales)} of {total_whales} whales this run"
        )

        min_usd = settings.WHALE_FUNDING_MIN_USD
        latest = await _rpc_call("eth_blockNumber", [])
        latest_int = _hex_to_int(latest) if latest else 0
        since = max(0, latest_int - 50_000)  # ~50k blocks ~= 40h window

        edges_added = 0
        for w in whales:
            try:
                txs = await _recent_outgoing_txs(w, since)
                for tx in txs:
                    from_addr = (tx.get("from") or "").lower()
                    to_addr = (tx.get("to") or "").lower()
                    if from_addr != w.wallet_address.lower():
                        continue
                    if not to_addr:
                        continue
                    value_wei = int(tx.get("value", "0") or 0)
                    amount_bnb = Decimal(value_wei) / Decimal(10**18)
                    amount_usd = float(amount_bnb) * bnb_price if bnb_price else 0.0
                    if amount_usd < min_usd:
                        continue

                    is_fresh, nonce = await _fresh_wallet(to_addr)
                    if not is_fresh:
                        continue

                    tx_hash = tx.get("hash", "")
                    existing = db.query(WalletFundingEdge).filter_by(
                        tx_hash=tx_hash
                    ).first()
                    if existing:
                        continue

                    edge = WalletFundingEdge(
                        chain="bsc",
                        funder_address=from_addr,
                        recipient_address=to_addr,
                        amount_native=amount_bnb,
                        amount_usd=Decimal(str(round(amount_usd, 2))),
                        tx_hash=tx_hash,
                        block_number=int(tx.get("blockNumber") or 0),
                        funded_at=datetime.utcnow(),
                        recipient_nonce_at_funding=nonce,
                        funder_label=w.label,
                        funder_role=w.role,
                    )
                    db.add(edge)
                    edges_added += 1
                    logger.warning(
                        f"WHALE→FRESH: {w.label} ({from_addr[:8]}) funded fresh "
                        f"{to_addr[:8]} with ${amount_usd:,.0f}"
                    )
            except Exception as e:
                logger.error(f"whale scan error on {w.wallet_address}: {e}")
            finally:
                # Free per-whale RPC response blobs before the next one
                gc.collect()

        db.commit()
        logger.info(f"Whale funding scanner complete. {edges_added} new edges.")
        return {"edges_added": edges_added, "whales_scanned": len(whales)}
    finally:
        db.close()


async def match_deploy_to_funding(db: Session, deploy: dict) -> None:
    """
    Phase B — called from deployer_watcher for every new deployment.
    If the deployer was funded by a whale recently, fire self-S signal.
    """
    deployer = (deploy.get("recipient") or "").lower()
    contract = (deploy.get("contract") or "").lower()
    window_start = datetime.utcnow() - timedelta(
        hours=settings.WHALE_FRESH_DEPLOY_WINDOW_HOURS
    )

    edges = db.query(WalletFundingEdge).filter(
        WalletFundingEdge.chain == "bsc",
        WalletFundingEdge.recipient_address == deployer,
        WalletFundingEdge.funded_at >= window_start,
    ).all()
    if not edges:
        return

    # Pick the most recent, largest funding edge as the primary evidence
    best = max(edges, key=lambda e: (e.amount_usd or Decimal(0)))
    best.recipient_deployed = True
    best.recipient_deploy_contract = contract
    best.recipient_deploy_tx = deploy.get("tx") or ""
    best.recipient_deploy_at = datetime.utcnow()

    record_signal(
        db,
        chain="bsc",
        contract_address=contract,
        signal_type="whale_fresh_wallet",
        signal_tier=TIER_SELF_S,   # SELF_S = 1 signal is enough for S-tier
        confidence=95,
        evidence={
            "deployer": deployer,
            "funder": best.funder_address,
            "funder_label": best.funder_label,
            "funder_role": best.funder_role,
            "funded_usd": str(best.amount_usd),
            "funded_tx": best.tx_hash,
            "funded_at": best.funded_at.isoformat() if best.funded_at else None,
            "deploy_tx": deploy.get("tx"),
        },
        description=(
            f"Fresh wallet {deployer[:8]} was funded ${best.amount_usd} "
            f"by {best.funder_label or best.funder_address[:8]} and deployed "
            f"a contract within {settings.WHALE_FRESH_DEPLOY_WINDOW_HOURS}h. "
            f"TRUMP pattern."
        ),
        deployer_address=deployer,
        deploy_tx_hash=deploy.get("tx"),
        deploy_block=deploy.get("block"),
        deployed_at=datetime.utcnow(),
    )


async def run_whale_fresh_watcher():
    """Scheduler entry — runs Phase A."""
    return await record_funding_edges()
