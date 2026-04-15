"""
Seed golden deployers (Module 1 prerequisite).

Pulls top BSC tokens by peak mcap from DeFi Llama + GeckoTerminal,
resolves their deployer addresses via MegaNode, tags via Arkham, and
inserts into known_wallets with role='golden_deployer'.

Run manually:  python scripts/seed_golden_deployers.py
Or via API:    POST /api/dashboard/seed-golden-deployers  (added later)
"""

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.arkham_client import lookup_address
from backend.bscscan_client import (
    TRANSFER_TOPIC,
    ZERO_ADDRESS,
    _chunked_get_logs,
    _unpad_address,
)
from backend.clients import defillama, geckoterminal
from backend.database import SessionLocal
from backend.models.known_wallet import KnownWallet

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("seed_golden_deployers")

# Seed-list threshold: we treat any BSC token with peak mcap above this as
# a "graduated" launch worth tagging its deployer.
MIN_PEAK_MCAP_USD = 50_000_000.0


async def _top_bsc_tokens() -> list[dict]:
    """Merge DeFi Llama + GeckoTerminal top lists on BSC."""
    # DeFi Llama protocols with a BSC chain + symbol
    protocols = await defillama.protocols()
    llama = []
    for p in protocols:
        chains = p.get("chains") or []
        if "BSC" not in chains and "bsc" not in [c.lower() for c in chains]:
            continue
        mcap = float(p.get("mcap") or 0)
        if mcap < MIN_PEAK_MCAP_USD:
            continue
        # We don't get the contract address reliably from llama; skip unless present
        addr = (p.get("address") or "").split(":")[-1]
        if not addr.startswith("0x"):
            continue
        llama.append({
            "address": addr.lower(),
            "symbol": p.get("symbol"),
            "name": p.get("name"),
            "peak_mcap": mcap,
            "source": "defillama",
        })

    # GeckoTerminal trending BSC pools for symbol/address resolution
    pools = await geckoterminal.trending_pools("bsc", limit=100)
    gt = []
    for pool in pools:
        attrs = pool.get("attributes") or {}
        rels = pool.get("relationships") or {}
        base_id = ((rels.get("base_token") or {}).get("data") or {}).get("id", "")
        addr = base_id.split("_", 1)[-1].lower() if base_id else ""
        if not addr.startswith("0x"):
            continue
        # GT doesn't give historical peak — use reserve_in_usd as rough proxy
        reserve = float(attrs.get("reserve_in_usd") or 0)
        if reserve < 1_000_000:
            continue
        gt.append({
            "address": addr,
            "symbol": (attrs.get("base_token_symbol") or "").upper(),
            "name": attrs.get("name"),
            "peak_mcap": reserve,
            "source": "geckoterminal",
        })

    seen = {}
    for entry in llama + gt:
        seen.setdefault(entry["address"], entry)
    return list(seen.values())


async def _deployer_of(contract: str) -> str | None:
    """First Transfer-from-zero recipient = deployer (approximation)."""
    logs = await _chunked_get_logs(
        {"address": contract.lower(), "topics": [TRANSFER_TOPIC, ZERO_ADDRESS]},
        total_blocks=200_000,
    )
    if not logs:
        return None
    first = logs[0]
    topics = first.get("topics") or []
    if len(topics) < 3:
        return None
    return _unpad_address(topics[2]).lower()


async def main():
    tokens = await _top_bsc_tokens()
    logger.info(f"Resolved {len(tokens)} candidate BSC tokens over threshold")
    db = SessionLocal()
    added = 0
    try:
        for t in tokens:
            try:
                deployer = await _deployer_of(t["address"])
                if not deployer:
                    logger.info(f"  {t['symbol']}: could not resolve deployer")
                    continue

                # Arkham lookup for label / entity
                label_parts = [f"{t['symbol']} deployer"]
                try:
                    ent = await lookup_address(deployer)
                    if ent and ent.get("name"):
                        label_parts.append(ent["name"])
                except Exception:
                    pass

                existing = db.query(KnownWallet).filter_by(wallet_address=deployer).first()
                if existing:
                    if existing.role != "golden_deployer":
                        existing.role = "golden_deployer"
                    if t["symbol"] and not existing.associated_token:
                        existing.associated_token = t["symbol"]
                        existing.associated_contract = t["address"]
                    continue

                w = KnownWallet(
                    wallet_address=deployer,
                    label=" — ".join(label_parts),
                    associated_token=t["symbol"],
                    associated_contract=t["address"],
                    role="golden_deployer",
                    is_active=True,
                    added_at=datetime.utcnow(),
                    notes=(
                        f"Seeded from {t['source']} — peak/reserve "
                        f"${t['peak_mcap']:,.0f}."
                    ),
                )
                db.add(w)
                db.flush()
                added += 1
                logger.info(f"  + {t['symbol']} deployer {deployer[:10]}…")
            except Exception as e:
                db.rollback()
                logger.warning(f"skip {t.get('symbol')}: {e}")
                continue
        db.commit()
        logger.info(f"Done. Added {added} new golden_deployer wallets.")
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
