"""
Known Wallet Tracker — Monitor confirmed pump operators.

THE HIGHEST-ALPHA FEATURE.
Follows known insider wallets to detect their next play:
- New token accumulations
- Gas funding to fresh wallets (new cluster setup)
- Exchange interactions
- Inter-cluster transfers

Runs every 30-60 minutes.

Dust filter: transfers whose estimated USD value is below
settings.WALLET_TRACK_MIN_USD are ignored. This kills the airdrop /
spam noise that previously flooded the activity feed and alert log.
"""

import logging
from datetime import datetime
from decimal import Decimal

import httpx
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import SessionLocal
from backend.bscscan_client import bscscan_request
from backend.models.flagged_token import FlaggedToken
from backend.models.known_wallet import KnownWallet
from backend.models.wallet_activity import WalletActivity
from backend.models.exchange_wallet import ExchangeWallet

logger = logging.getLogger(__name__)

# In-process price/symbol cache. Keyed by lowercase contract address.
# Cleared on process restart — that's fine, it just refills from DEX Screener.
_TOKEN_META_CACHE: dict[str, dict] = {}


async def get_token_meta(contract_address: str) -> dict:
    """
    Resolve {symbol, name, price_usd, decimals} for a token contract.
    First checks the FlaggedToken table; falls back to DEX Screener.
    Cached in memory to avoid hammering DEX Screener.
    """
    addr = contract_address.lower()
    if addr in _TOKEN_META_CACHE:
        return _TOKEN_META_CACHE[addr]

    meta = {"symbol": None, "name": None, "price_usd": None, "decimals": 18}

    # First try local DB
    db = SessionLocal()
    try:
        flagged = db.query(FlaggedToken).filter_by(contract_address=addr).first()
        if flagged:
            meta["symbol"] = flagged.token_symbol
            meta["name"] = flagged.token_name
            if flagged.price_usd:
                try:
                    meta["price_usd"] = float(flagged.price_usd)
                except Exception:
                    pass
    finally:
        db.close()

    # Fall back to DEX Screener for price (and to populate symbol if needed)
    if meta["price_usd"] is None or meta["symbol"] is None:
        try:
            url = f"{settings.DEXSCREENER_BASE_URL}/latest/dex/tokens/{addr}"
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    pairs = (resp.json() or {}).get("pairs") or []
                    bsc_pairs = [p for p in pairs if p.get("chainId") == "bsc"]
                    if bsc_pairs:
                        # Use deepest-liquidity pair
                        best = max(
                            bsc_pairs,
                            key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0),
                        )
                        if meta["symbol"] is None:
                            meta["symbol"] = best.get("baseToken", {}).get("symbol")
                            meta["name"] = best.get("baseToken", {}).get("name")
                        try:
                            meta["price_usd"] = float(best.get("priceUsd") or 0) or None
                        except Exception:
                            pass
        except Exception as e:
            logger.debug(f"DEX Screener meta fetch failed for {addr[:10]}: {e}")

    _TOKEN_META_CACHE[addr] = meta
    return meta


async def get_recent_token_transfers(wallet_address: str) -> list[dict]:
    """Get the 50 most recent token transfers for a wallet."""
    data = await bscscan_request({
        "module": "account",
        "action": "tokentx",
        "address": wallet_address,
        "page": "1",
        "offset": "50",
        "sort": "desc",
    })
    if data and isinstance(data.get("result"), list):
        return data["result"]
    return []


async def get_recent_normal_transactions(wallet_address: str) -> list[dict]:
    """Get recent normal (BNB) transactions for gas funding detection."""
    data = await bscscan_request({
        "module": "account",
        "action": "txlist",
        "address": wallet_address,
        "page": "1",
        "offset": "20",
        "sort": "desc",
    })
    if data and isinstance(data.get("result"), list):
        return data["result"]
    return []


def get_known_token_holdings(db: Session, wallet_address: str) -> set[str]:
    """Get set of token contracts this wallet has previously interacted with."""
    activities = db.query(WalletActivity.token_contract).filter(
        WalletActivity.wallet_address == wallet_address,
        WalletActivity.token_contract.isnot(None),
    ).distinct().all()
    return {a[0].lower() for a in activities if a[0]}


async def track_wallet(
    wallet: KnownWallet,
    db: Session,
    exchange_addresses: set[str],
    known_addresses: set[str],
) -> list[dict]:
    """
    Track a single known wallet's recent activity.
    Returns list of notable activities detected.

    Filters out dust transfers (USD value below WALLET_TRACK_MIN_USD)
    so airdrop spam and tiny test sends do not pollute the activity feed
    or auto-flag bogus tokens.
    """
    notable = []
    wallet_addr = wallet.wallet_address.lower()
    known_holdings = get_known_token_holdings(db, wallet_addr)
    min_usd = float(settings.WALLET_TRACK_MIN_USD or 0)

    # 1. Check token transfers
    token_transfers = await get_recent_token_transfers(wallet_addr)
    for tx in token_transfers:
        from_addr = tx.get("from", "").lower()
        to_addr = tx.get("to", "").lower()
        token_contract = tx.get("contractAddress", "").lower()
        tx_hash = tx.get("hash", "")
        block = int(tx.get("blockNumber", 0))
        if not token_contract or not tx_hash:
            continue

        # Skip if already logged (cheap check first to avoid a DEX Screener call)
        existing = db.query(WalletActivity).filter_by(tx_hash=tx_hash).first()
        if existing:
            continue

        # Resolve token metadata (symbol, price, decimals).
        # If the bscscan_client doesn't return decimals (eth_getLogs path),
        # default to 18 — but use DEX-Screener-resolved symbol/price.
        meta = await get_token_meta(token_contract)
        token_symbol = tx.get("tokenSymbol") or meta.get("symbol") or ""
        decimals = int(tx.get("tokenDecimal", "18") or 18)

        try:
            amount = Decimal(tx.get("value", "0")) / Decimal(10**decimals)
        except Exception:
            amount = Decimal("0")

        # USD value (None when price unknown)
        value_usd: Decimal | None = None
        if meta.get("price_usd"):
            try:
                value_usd = (amount * Decimal(str(meta["price_usd"]))).quantize(Decimal("0.01"))
            except Exception:
                value_usd = None

        # DUST FILTER: skip transfers whose USD value is known and below the
        # threshold. If price is unknown, we can't tell — be conservative and
        # only let it through if `amount` is large in absolute terms (>=1000
        # tokens) to avoid logging 0.0000001 dust.
        if value_usd is not None:
            if float(value_usd) < min_usd:
                continue
        else:
            if amount < Decimal("1000"):
                continue

        # Determine activity type
        activity_type = None
        is_new = False
        flagged = False
        counterparty = ""
        counterparty_label = ""

        if from_addr == wallet_addr:
            # Outgoing transfer
            if to_addr in exchange_addresses:
                activity_type = "exchange_deposit"
                counterparty = to_addr
                ex = db.query(ExchangeWallet).filter_by(wallet_address=to_addr).first()
                counterparty_label = ex.exchange_name if ex else "Unknown Exchange"
                flagged = True
            elif to_addr in known_addresses:
                activity_type = "transfer_out"
                counterparty = to_addr
                counterparty_label = "Known wallet"
            else:
                activity_type = "sell" if to_addr != wallet_addr else "transfer_out"
                counterparty = to_addr

        elif to_addr == wallet_addr:
            # Incoming transfer
            if from_addr in exchange_addresses:
                activity_type = "exchange_withdrawal"
                counterparty = from_addr
                ex = db.query(ExchangeWallet).filter_by(wallet_address=from_addr).first()
                counterparty_label = ex.exchange_name if ex else "Unknown Exchange"
                flagged = True
            else:
                activity_type = "buy"
                counterparty = from_addr

            # Check if this is a NEW token for this wallet
            if token_contract not in known_holdings:
                is_new = True
                activity_type = "new_token_accumulation"
                flagged = True

        if not activity_type:
            continue

        # Log the activity
        activity = WalletActivity(
            wallet_address=wallet_addr,
            activity_type=activity_type,
            token_contract=token_contract,
            token_symbol=token_symbol,
            amount=amount,
            value_usd=value_usd,
            counterparty=counterparty,
            counterparty_label=counterparty_label,
            tx_hash=tx_hash,
            block_number=block,
            detected_at=datetime.utcnow(),
            is_new_token=is_new,
            flagged=flagged,
        )
        db.add(activity)

        if flagged:
            notable.append({
                "wallet": wallet_addr,
                "wallet_label": wallet.label,
                "activity": activity_type,
                "token_contract": token_contract,
                "token_symbol": token_symbol,
                "amount": str(amount),
                "value_usd": str(value_usd) if value_usd is not None else None,
                "is_new_token": is_new,
            })

    # 2. Check normal transactions for gas funding to new wallets
    normal_txs = await get_recent_normal_transactions(wallet_addr)
    for tx in normal_txs:
        from_addr = tx.get("from", "").lower()
        to_addr = tx.get("to", "").lower()
        value = Decimal(tx.get("value", "0")) / Decimal(10**18)  # BNB has 18 decimals
        tx_hash = tx.get("hash", "")
        block = int(tx.get("blockNumber", 0))

        if from_addr != wallet_addr:
            continue
        if value < Decimal("0.001"):
            continue  # Skip dust

        # Skip if already logged
        existing = db.query(WalletActivity).filter_by(tx_hash=tx_hash).first()
        if existing:
            continue

        # Small BNB transfer to a new address = possible gas funding for cluster
        if value < Decimal("1") and to_addr not in known_addresses and to_addr not in exchange_addresses:
            activity = WalletActivity(
                wallet_address=wallet_addr,
                activity_type="gas_funding",
                token_contract=None,
                token_symbol="BNB",
                amount=value,
                counterparty=to_addr,
                counterparty_label="New wallet (potential cluster)",
                tx_hash=tx_hash,
                block_number=block,
                detected_at=datetime.utcnow(),
                is_new_token=False,
                flagged=True,
            )
            db.add(activity)
            notable.append({
                "wallet": wallet_addr,
                "wallet_label": wallet.label,
                "activity": "gas_funding",
                "token_contract": None,
                "token_symbol": "BNB",
                "amount": str(value),
                "funded_wallet": to_addr,
            })

    db.commit()
    return notable


async def auto_flag_new_token(db: Session, token_contract: str, detected_via: str):
    """
    Automatically flag a token discovered through wallet tracking.

    Populates as much metadata as we can resolve up-front (symbol, name,
    price) so the dashboard never sees a bare "?" stub. The volume scanner
    and profile checker will fill in volume/holder/contract details on
    their next runs.
    """
    existing = db.query(FlaggedToken).filter_by(contract_address=token_contract).first()
    if existing:
        return

    meta = await get_token_meta(token_contract)
    flagged = FlaggedToken(
        contract_address=token_contract,
        chain="bsc",
        token_name=meta.get("name"),
        token_symbol=meta.get("symbol"),
        price_usd=Decimal(str(meta["price_usd"])) if meta.get("price_usd") else None,
        first_flagged_at=datetime.utcnow(),
        last_seen_at=datetime.utcnow(),
        status="raw",
    )
    db.add(flagged)
    db.commit()
    logger.warning(
        f"Auto-flagged {meta.get('symbol') or token_contract[:10]} "
        f"({token_contract[:10]}...) via {detected_via}"
    )


async def run_wallet_tracker():
    """
    Main wallet tracker entry point. Called every 30 minutes.

    Memory + time discipline: with 500+ known wallets and ~10 RPC calls
    per wallet (getting tokentx + normal txs + per-transfer DEX Screener
    meta lookups), scanning ALL wallets in one run easily exceeds the
    30-min interval and OOMs the 2GB container. We process a random
    subset of WALLET_TRACK_BATCH_SIZE wallets per run — over ~12 runs
    (6 hours) we cover the full set with high probability.
    """
    import gc
    import random as _random

    logger.info("Starting wallet tracker run...")
    db = SessionLocal()
    tracked_count = 0
    all_notable = []

    try:
        # Load reference data
        total_active = db.query(KnownWallet).filter(
            KnownWallet.is_active.is_(True)
        ).count()

        # Random subset per run; ORDER BY RANDOM() is slow on huge tables
        # but known_wallets is <10k rows so it's fine.
        from sqlalchemy import func as _sql_func
        active_wallets = (
            db.query(KnownWallet)
            .filter(KnownWallet.is_active.is_(True))
            .order_by(_sql_func.random())
            .limit(settings.WALLET_TRACK_BATCH_SIZE)
            .all()
        )
        exchange_addresses = {
            w.wallet_address.lower()
            for w in db.query(ExchangeWallet).all()
        }
        # `known_addresses` is used to label intra-cluster transfers as
        # "Known wallet" — needs the full set, not just the batch
        known_addresses = {
            w.wallet_address.lower()
            for w in db.query(KnownWallet).filter(KnownWallet.is_active.is_(True)).all()
        }

        logger.info(
            f"Tracking {len(active_wallets)} of {total_active} known wallets this run"
        )

        for wallet in active_wallets:
            try:
                notable = await track_wallet(wallet, db, exchange_addresses, known_addresses)
                tracked_count += 1
                all_notable.extend(notable)

                # Auto-flag tokens from new accumulations — but only if the
                # accumulation has a known USD value above the dust threshold.
                # This prevents the FlaggedToken table from filling up with
                # junk addresses that nobody actually traded.
                min_usd = float(settings.WALLET_TRACK_MIN_USD or 0)
                for item in notable:
                    if item["activity"] != "new_token_accumulation":
                        continue
                    if not item.get("token_contract"):
                        continue
                    raw_usd = item.get("value_usd")
                    try:
                        usd = float(raw_usd) if raw_usd is not None else None
                    except Exception:
                        usd = None
                    # Require a known USD value above the threshold before
                    # creating a FlaggedToken stub.
                    if usd is None or usd < min_usd:
                        continue
                    await auto_flag_new_token(
                        db,
                        item["token_contract"],
                        f"known operator {item['wallet_label']}",
                    )

            except Exception as e:
                logger.error(f"Error tracking wallet {wallet.wallet_address}: {e}")
                continue
            finally:
                # Free per-wallet RPC response objects before the next wallet
                # so memory doesn't accumulate across the batch.
                gc.collect()

        logger.info(
            f"Wallet tracker complete. Tracked {tracked_count} wallets, "
            f"{len(all_notable)} notable activities."
        )

        for item in all_notable:
            logger.warning(
                f"NOTABLE: {item['wallet_label']} — {item['activity']} — "
                f"{item.get('token_symbol', 'BNB')} ({item.get('amount', '')})"
            )

        return all_notable

    except Exception as e:
        logger.error(f"Wallet tracker error: {e}")
        return []
    finally:
        db.close()
