"""Jupiter DCA Order Watcher — detect large DCA orders on low-cap Solana tokens.

Every cycle:
  1. Poll the Jupiter DCA program for recent openDca / openDcaV2 txs.
  2. Parse Anchor instruction data: user wallet, input/output mints, DCA size.
  3. Resolve output token price + market cap via DexScreener.
  4. Filter: DCA value >= threshold AND market cap < ceiling.
  5. Classify wallet (fresh/dormant) + check CEX funding.
  6. Fire Telegram alert with full context.

Catches the "someone placed a huge DCA order on a low-cap token" signal —
the kind of move insiders make weeks before a pump (cf. USDUC pattern:
fresh wallets funded from Binance accumulated 5% supply before listing).
"""

import asyncio
import hashlib
import json
import logging
import struct
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from backend import settings_cache
from backend.alerts.telegram_bot import fire_dca_order_alert
from backend.anomaly.wallet_classifier import classify_wallet
from backend.clients import dexscreener, defillama, helius
from backend.database import SessionLocal
from backend.models.alert import Alert
from backend.models.chain_anomaly import ChainAnomaly
from backend.models.cex_address import CexAddress

logger = logging.getLogger(__name__)

# Jupiter DCA Program ID (v2, active on mainnet)
DCA_PROGRAM_ID = "DCA265Vj8a9CEuX1eb1LWRnDT7uK6q1xMipnNyatn23M"

# Anchor discriminators — sha256("global:<fn_name>")[:8]
_OPEN_DCA_DISC = hashlib.sha256(b"global:open_dca").digest()[:8]
_OPEN_DCA_V2_DISC = hashlib.sha256(b"global:open_dca_v2").digest()[:8]
_KNOWN_DISCS = {_OPEN_DCA_DISC, _OPEN_DCA_V2_DISC}

# Known input tokens (SOL + stables) — maps mint → (symbol, decimals)
_INPUT_TOKENS = {
    "So11111111111111111111111111111111111111112": ("SOL", 9),
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": ("USDC", 6),
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": ("USDT", 6),
}

SIGS_PER_CYCLE = 50
LAMPORTS_PER_SOL = 1_000_000_000

# Base58 alphabet (no 0, O, I, l)
_B58 = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _cfg(key, default):
    return settings_cache.get(key, default)


# ─── Base58 decoder ──────────────────────────────────────────────────

def _b58decode(s: str) -> bytes:
    n = 0
    for c in s.encode():
        n = n * 58 + _B58.index(c)
    result = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + result


# ─── Transaction parsing ─────────────────────────────────────────────

def _resolve_account_keys(tx: dict) -> list[str]:
    message = (tx.get("transaction") or {}).get("message") or {}
    keys = []
    for k in message.get("accountKeys") or []:
        pk = k.get("pubkey") if isinstance(k, dict) else str(k)
        keys.append(pk or "")
    return keys


def _try_parse_dca_ix(ix: dict, account_keys: list[str]) -> dict | None:
    if not isinstance(ix, dict):
        return None
    if (ix.get("programId") or "") != DCA_PROGRAM_ID:
        return None

    data_b58 = ix.get("data")
    raw_accounts = ix.get("accounts") or []
    if not data_b58 or len(raw_accounts) < 5:
        return None

    accounts = []
    for a in raw_accounts:
        if isinstance(a, int):
            accounts.append(account_keys[a] if a < len(account_keys) else "")
        else:
            accounts.append(str(a))
    if len(accounts) < 5:
        return None

    try:
        data = _b58decode(data_b58)
    except Exception:
        return None
    if len(data) < 24:
        return None

    disc = data[:8]
    if disc not in _KNOWN_DISCS:
        return None

    in_amount_raw = struct.unpack_from("<Q", data, 16)[0]
    in_amount_per_cycle = struct.unpack_from("<Q", data, 24)[0] if len(data) >= 32 else 0
    cycle_frequency = struct.unpack_from("<q", data, 32)[0] if len(data) >= 40 else 0

    return {
        "user": accounts[1],
        "input_mint": accounts[3],
        "output_mint": accounts[4],
        "in_amount_raw": in_amount_raw,
        "in_amount_per_cycle": in_amount_per_cycle,
        "cycle_frequency": cycle_frequency,
        "disc_type": "open_dca_v2" if disc == _OPEN_DCA_V2_DISC else "open_dca",
    }


def _parse_dca_from_tx(tx: dict) -> dict | None:
    account_keys = _resolve_account_keys(tx)
    message = (tx.get("transaction") or {}).get("message") or {}

    for ix in message.get("instructions") or []:
        result = _try_parse_dca_ix(ix, account_keys)
        if result:
            return result

    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        for ix in inner.get("instructions") or []:
            result = _try_parse_dca_ix(ix, account_keys)
            if result:
                return result
    return None


def _amount_to_usd(raw: int, mint: str, sol_price: float) -> float:
    info = _INPUT_TOKENS.get(mint)
    if not info:
        return 0.0
    name, decimals = info
    human = raw / (10 ** decimals)
    return human * sol_price if name == "SOL" else human


# ─── CEX funding check ───────────────────────────────────────────────

async def _check_cex_funding(
    wallet: str, cex_addrs: set[str],
) -> dict | None:
    """Check if wallet recently received SOL from a known CEX address.
    Returns {cex_address, amount_sol} or None."""
    if not helius.configured:
        return None
    sigs = await helius.get_signatures(wallet, limit=15)
    if not sigs:
        return None

    for sig_meta in sigs[:10]:
        sig = sig_meta.get("signature")
        if not sig:
            continue
        tx = await helius.get_transaction(sig)
        if not tx:
            continue
        try:
            message = (tx.get("transaction") or {}).get("message") or {}
            meta = tx.get("meta") or {}
            account_keys = message.get("accountKeys") or []

            keys = []
            for k in account_keys:
                pk = k.get("pubkey") if isinstance(k, dict) else str(k)
                keys.append(pk)

            pre_sol = meta.get("preBalances") or []
            post_sol = meta.get("postBalances") or []

            wallet_idx = None
            for i, k in enumerate(keys):
                if k == wallet:
                    wallet_idx = i
                    break
            if wallet_idx is None:
                continue
            if wallet_idx >= len(pre_sol) or wallet_idx >= len(post_sol):
                continue

            sol_in = (post_sol[wallet_idx] - pre_sol[wallet_idx]) / LAMPORTS_PER_SOL
            if sol_in <= 0.1:
                continue

            for i, k in enumerate(keys):
                if k in cex_addrs and i < len(pre_sol) and i < len(post_sol):
                    cex_out = (pre_sol[i] - post_sol[i]) / LAMPORTS_PER_SOL
                    if cex_out > 0.1:
                        return {"cex_address": k, "amount_sol": sol_in}
        except Exception:
            continue
    return None


# ─── Main entry ──────────────────────────────────────────────────────

async def run_dca_order_watcher() -> dict:
    if not helius.configured:
        return {"skipped": "no_helius_key"}
    if not _cfg("DCA_ORDER_ENABLED", True):
        return {"skipped": "disabled"}

    min_value = float(_cfg("DCA_ORDER_MIN_VALUE_USD", 150_000.0))
    max_mc = float(_cfg("DCA_ORDER_MAX_MC_USD", 50_000_000.0))
    dedup_hours = int(_cfg("DCA_ORDER_DEDUP_HOURS", 24))

    db = SessionLocal()
    try:
        sol_price = 0.0
        try:
            prices = await defillama.current_prices(["coingecko:solana"])
            sol_price = float(
                (prices.get("coingecko:solana") or {}).get("price") or 0
            )
        except Exception:
            pass
        if sol_price == 0:
            return {"skipped": "no_sol_price"}

        cex_rows = db.query(CexAddress).filter(
            CexAddress.is_active.is_(True),
        ).all()
        cex_addrs = {r.address for r in cex_rows}
        cex_by_addr = {r.address: r for r in cex_rows}

        cutoff = datetime.utcnow() - timedelta(hours=dedup_hours)
        already_alerted = {
            r[0]
            for r in db.query(Alert.contract_address)
            .filter(Alert.alert_type == "dca_order", Alert.fired_at >= cutoff)
            .all()
        }

        sigs = await helius.get_signatures(DCA_PROGRAM_ID, limit=SIGS_PER_CYCLE)
        if not sigs:
            return {"checked": 0}

        orders_found = 0
        alerts_fired = 0

        for sig_meta in sigs:
            sig = sig_meta.get("signature")
            if not sig:
                continue

            existing = db.query(ChainAnomaly).filter_by(
                source="jupiter_dca", tx_hash=sig,
            ).first()
            if existing:
                continue

            tx = await helius.get_transaction(sig)
            if not tx:
                continue

            dca = _parse_dca_from_tx(tx)
            if not dca:
                continue

            value_usd = _amount_to_usd(
                dca["in_amount_raw"], dca["input_mint"], sol_price,
            )
            if value_usd < min_value:
                continue

            orders_found += 1
            output_mint = dca["output_mint"]

            if output_mint in already_alerted:
                continue

            mkt = await dexscreener.token_info(output_mint)
            mc = (mkt or {}).get("market_cap_usd") or 0
            if mc > 0 and mc > max_mc:
                continue

            cls = await classify_wallet(dca["user"], db)
            cex_info = await _check_cex_funding(dca["user"], cex_addrs)

            input_info = _INPUT_TOKENS.get(dca["input_mint"], ("UNK", 9))
            input_name, input_decimals = input_info
            human_amount = dca["in_amount_raw"] / (10 ** input_decimals)

            per_cycle = 0.0
            if dca["in_amount_per_cycle"]:
                per_cycle = dca["in_amount_per_cycle"] / (10 ** input_decimals)
            num_cycles = int(human_amount / per_cycle) if per_cycle > 0 else 0
            cycle_freq_sec = dca["cycle_frequency"]

            block_time = tx.get("blockTime")
            event_ts = (
                datetime.utcfromtimestamp(block_time)
                if block_time else datetime.utcnow()
            )

            cex_label = None
            if cex_info:
                obj = cex_by_addr.get(cex_info["cex_address"])
                cex_label = obj.name if obj else cex_info["cex_address"][:8]

            event = {
                "mint": output_mint,
                "symbol": (mkt or {}).get("symbol"),
                "name": (mkt or {}).get("name"),
                "market": mkt or {},
                "user_wallet": dca["user"],
                "input_token": input_name,
                "dca_value_usd": value_usd,
                "dca_amount": human_amount,
                "per_cycle": per_cycle,
                "num_cycles": num_cycles,
                "cycle_freq_sec": cycle_freq_sec,
                "signature": sig,
                "is_fresh": cls.get("is_fresh", False),
                "is_dormant": cls.get("is_dormant", False),
                "tx_count": cls.get("tx_count_observed", 0),
                "cex_funded": cex_info is not None,
                "cex_label": cex_label,
                "cex_amount_sol": cex_info["amount_sol"] if cex_info else 0,
                "event_ts": event_ts,
            }

            try:
                anom = ChainAnomaly(
                    source="jupiter_dca",
                    event_type="dca_order",
                    coin=(mkt or {}).get("symbol") or output_mint[:8],
                    side="buy",
                    notional_usd=Decimal(str(round(value_usd, 2))),
                    actor_address=dca["user"],
                    is_fresh_wallet=cls.get("is_fresh", False),
                    actor_history_count=cls.get("tx_count_observed"),
                    tx_hash=sig,
                    extra_json=json.dumps({
                        "input_token": input_name,
                        "dca_amount": human_amount,
                        "mc_usd": mc,
                        "cex_funded": cex_info is not None,
                        "cex_label": cex_label,
                    }),
                    is_alerted=False,
                )
                db.add(anom)
                db.commit()
            except Exception as e:
                logger.error(f"DCA anomaly persist failed: {e}")
                db.rollback()
                continue

            alert_id = await fire_dca_order_alert(event, db=db)
            if alert_id:
                alerts_fired += 1
                already_alerted.add(output_mint)
                try:
                    anom.is_alerted = True
                    anom.alert_id = alert_id
                    db.commit()
                except Exception:
                    db.rollback()

        return {
            "checked_sigs": len(sigs),
            "orders_found": orders_found,
            "alerts_fired": alerts_fired,
        }
    finally:
        db.close()
