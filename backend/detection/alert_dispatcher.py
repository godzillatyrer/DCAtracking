"""
Alert Dispatcher — runs after the wallet activity tracker to check
for fresh signals worth pushing to Telegram.

Two alert flavors:
  - CABAL_CONVERGENCE: cluster score >= threshold in the window
  - SMART_MONEY_SOLO:  a top-ranked wallet (by confidence_score) just
                       bought a mint it's never held before

Dedupe: we don't re-fire an alert_type for the same mint within 24h.
Stored as rows in the `alerts` table (alert_type + contract_address +
fired_at).
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.alerts.telegram_bot import fire_cabal_alert, send_telegram_message
from backend.database import SessionLocal
from backend.models.alert import Alert
from backend.models.solana_known_wallet import SolanaKnownWallet
from backend.models.solana_wallet_activity import SolanaWalletActivity
from backend.models.solana_wallet_stats import SolanaWalletStats

logger = logging.getLogger(__name__)

# Don't re-alert the same (mint, type) within this many hours
_DEDUP_HOURS = 24

# Convergence: consider mints bought in the last N minutes
CONVERGENCE_WINDOW_MIN = 60
# Minimum raw wallet count for a convergence candidate
CONVERGENCE_MIN_WALLETS = 2
# Score threshold (time-tightness * wallet confidence sum) to alert on
CONVERGENCE_MIN_SCORE = 2.0

# Solo alerts: only from wallets at or above this percentile in
# confidence_score. With e.g. 500 tracked wallets, top 5% = 25 wallets.
SOLO_TOP_PERCENTILE = 5  # top 5% only
SOLO_MIN_CONFIDENCE = 3.0  # absolute floor regardless of percentile


def _recently_alerted_mints(db: Session, alert_type: str) -> set[str]:
    cutoff = datetime.utcnow() - timedelta(hours=_DEDUP_HOURS)
    rows = db.query(Alert.contract_address).filter(
        Alert.alert_type == alert_type,
        Alert.fired_at >= cutoff,
    ).all()
    return {r[0] for r in rows}


# ─── Convergence detection (Python; same logic as /convergence API) ───

def _score_cluster(wallets: list[dict]) -> float:
    """Confidence-weighted count * time-tightness multiplier."""
    import math
    base = sum(w["weight"] for w in wallets)
    # Time-tightness: a 90-second convergence is worth more than a
    # 60-minute one. Multiplier = 1 / (1 + span_minutes / 10).
    times = [w["last_buy"] for w in wallets if w["last_buy"]]
    if len(times) >= 2:
        span_min = (max(times) - min(times)).total_seconds() / 60.0
        tightness = 1.0 / (1.0 + span_min / 10.0)
    else:
        tightness = 1.0
    return round(base * (1.0 + tightness) / 2.0, 2)


def _wallet_weight(role: str | None, confidence: float | None,
                   entity_size: int | None) -> float:
    """Per-wallet contribution to a cluster score.

    Role gives a floor; confidence amplifies. Big entities get their
    contributions divided so 5 sockpuppets count as 1.
    """
    role_w = {
        "cabal_trader": 1.0,
        "cabal_linked": 0.7,
        "cabal_linked_2": 0.5,
    }.get(role or "", 0.5)
    # Confidence bump: 0 → 1x, 5 → 1.5x, 10 → 2x, capped
    conf_bump = 1.0 + min(float(confidence or 0), 10.0) / 10.0
    # Anti-Sybil: divide by sqrt of entity size (5 wallets → /2.23)
    import math
    sybil_div = math.sqrt(max(entity_size or 1, 1))
    return round(role_w * conf_bump / sybil_div, 3)


def _detect_convergences(db: Session, since: datetime) -> list[dict]:
    rows = (
        db.query(
            SolanaWalletActivity.token_mint,
            SolanaWalletActivity.wallet_address,
            func.min(SolanaWalletActivity.detected_at).label("first_buy"),
            func.max(SolanaWalletActivity.detected_at).label("last_buy"),
            func.sum(SolanaWalletActivity.value_usd).label("value_usd"),
            SolanaKnownWallet.role,
            SolanaWalletStats.confidence_score,
            SolanaWalletStats.entity_size,
        )
        .outerjoin(
            SolanaKnownWallet,
            SolanaKnownWallet.wallet_address == SolanaWalletActivity.wallet_address,
        )
        .outerjoin(
            SolanaWalletStats,
            SolanaWalletStats.wallet_address == SolanaWalletActivity.wallet_address,
        )
        .filter(
            SolanaWalletActivity.detected_at >= since,
            SolanaWalletActivity.activity_type == "spl_buy",
            SolanaWalletActivity.token_mint.isnot(None),
        )
        .group_by(
            SolanaWalletActivity.token_mint,
            SolanaWalletActivity.wallet_address,
            SolanaKnownWallet.role,
            SolanaWalletStats.confidence_score,
            SolanaWalletStats.entity_size,
        )
        .all()
    )

    clusters: dict[str, list[dict]] = {}
    for mint, wallet, first_buy, last_buy, value_usd, role, conf, esize in rows:
        clusters.setdefault(mint, []).append({
            "wallet": wallet,
            "role": role,
            "first_buy": first_buy,
            "last_buy": last_buy,
            "value_usd": float(value_usd or 0),
            "confidence": float(conf or 0),
            "entity_size": int(esize or 1),
            "weight": _wallet_weight(role, conf, esize),
        })

    # Deduplicate wallets within the same entity: keep the highest-
    # weight wallet per entity_id (we approximate by grouping same-
    # sized entity wallets; for simplicity at this stage we DON'T
    # actually dedup by entity_id here — the sqrt(entity_size) divisor
    # in the weight already de-values each wallet enough).

    out = []
    for mint, wallets in clusters.items():
        if len(wallets) < CONVERGENCE_MIN_WALLETS:
            continue
        score = _score_cluster(wallets)
        if score < CONVERGENCE_MIN_SCORE:
            continue
        out.append({
            "mint": mint,
            "wallets": wallets,
            "score": score,
        })
    out.sort(key=lambda c: -c["score"])
    return out


# ─── Solo detection ───────────────────────────────────────────────────

def _top_wallets(db: Session) -> set[str]:
    """Wallets at or above the solo alert threshold."""
    total = db.query(func.count(SolanaWalletStats.wallet_address)).scalar() or 0
    if total == 0:
        return set()
    limit = max(5, int(total * SOLO_TOP_PERCENTILE / 100))
    rows = (
        db.query(SolanaWalletStats.wallet_address)
        .filter(SolanaWalletStats.confidence_score >= SOLO_MIN_CONFIDENCE)
        .order_by(SolanaWalletStats.confidence_score.desc())
        .limit(limit)
        .all()
    )
    return {r[0] for r in rows}


def _detect_solo_buys(db: Session, since: datetime) -> list[dict]:
    """Top-ranked wallets that just bought a NEW mint."""
    top = _top_wallets(db)
    if not top:
        return []
    rows = (
        db.query(
            SolanaWalletActivity.wallet_address,
            SolanaWalletActivity.token_mint,
            SolanaWalletActivity.value_usd,
            SolanaWalletActivity.detected_at,
            SolanaKnownWallet.role,
            SolanaWalletStats.confidence_score,
        )
        .outerjoin(
            SolanaKnownWallet,
            SolanaKnownWallet.wallet_address == SolanaWalletActivity.wallet_address,
        )
        .outerjoin(
            SolanaWalletStats,
            SolanaWalletStats.wallet_address == SolanaWalletActivity.wallet_address,
        )
        .filter(
            SolanaWalletActivity.activity_type == "spl_buy",
            SolanaWalletActivity.is_new_token.is_(True),
            SolanaWalletActivity.detected_at >= since,
            SolanaWalletActivity.wallet_address.in_(top),
        )
        .order_by(SolanaWalletActivity.detected_at.desc())
        .all()
    )
    return [
        {
            "wallet": addr, "mint": mint, "value_usd": float(val or 0),
            "detected_at": detected, "role": role, "confidence": float(conf or 0),
        }
        for (addr, mint, val, detected, role, conf) in rows
    ]


# ─── Formatters ───────────────────────────────────────────────────────

def _short(s: str) -> str:
    return f"{s[:6]}…{s[-4:]}" if s and len(s) > 12 else (s or "")


async def _fire_solo(db: Session, event: dict):
    mint = event["mint"]
    wallet = event["wallet"]
    already = db.query(Alert.id).filter(
        Alert.alert_type == "smart_money_solo",
        Alert.contract_address == mint,
        Alert.fired_at >= datetime.utcnow() - timedelta(hours=_DEDUP_HOURS),
    ).first()
    if already:
        return False

    msg = (
        f"💎 <b>Smart money solo buy</b>\n\n"
        f"<b>Wallet:</b> <code>{_short(wallet)}</code> "
        f"(confidence {event['confidence']:.1f}, role {event['role']})\n"
        f"<b>Mint:</b> <code>{mint}</code>\n"
        f"<b>Size:</b> ${event['value_usd']:,.0f}\n\n"
        f'<a href="https://dexscreener.com/solana/{mint}">DEX Screener</a> | '
        f'<a href="https://gmgn.ai/sol/token/{mint}">GMGN</a> | '
        f'<a href="https://solscan.io/account/{wallet}">Wallet</a>'
    )
    msg_id = await send_telegram_message(msg)
    db.add(Alert(
        contract_address=mint,
        alert_type="smart_money_solo",
        trigger_reason=(
            f"Top-confidence wallet {_short(wallet)} "
            f"(score {event['confidence']:.1f}) bought NEW mint."
        ),
        telegram_sent=msg_id is not None,
        telegram_message_id=msg_id,
        fired_at=datetime.utcnow(),
    ))
    db.commit()
    return True


# ─── Entry point ──────────────────────────────────────────────────────

async def run_alert_dispatch():
    """Called after each activity tracker run (and safe to run standalone)."""
    db = SessionLocal()
    try:
        since = datetime.utcnow() - timedelta(minutes=CONVERGENCE_WINDOW_MIN)
        already_conv = _recently_alerted_mints(db, "cabal_convergence")

        fired_conv = 0
        for cluster in _detect_convergences(db, since):
            mint = cluster["mint"]
            if mint in already_conv:
                continue
            wallets = [w["wallet"] for w in cluster["wallets"]]
            reason = (
                f"{len(wallets)} tracked wallets bought in "
                f"{CONVERGENCE_WINDOW_MIN}m. Score: {cluster['score']:.2f}."
            )
            await fire_cabal_alert(
                mint=mint, symbol="",
                buyer_wallets=wallets,
                trigger_reason=reason,
                db=db,
            )
            fired_conv += 1

        fired_solo = 0
        for event in _detect_solo_buys(db, since):
            if await _fire_solo(db, event):
                fired_solo += 1

        if fired_conv or fired_solo:
            logger.info(
                f"alert_dispatch: fired {fired_conv} convergence + "
                f"{fired_solo} solo alerts"
            )
        return {"convergence_fired": fired_conv, "solo_fired": fired_solo}
    finally:
        db.close()
