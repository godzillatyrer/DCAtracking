"""
Launch alert — the ONE path for Telegram alerts originating from the
launch-detection pipeline. Called exclusively by launch_scorer when a
candidate crosses into tier S or A.

No module should import or call this directly. The tiered scorer is
the single decision point, and this function is responsible only for
formatting + delivery.
"""

import logging
from datetime import datetime

from backend.alerts.telegram_bot import (
    get_alerts_fired_today,
    send_telegram_message,
)
from backend.config import settings
from backend.database import SessionLocal
from backend.models.alert import Alert
from backend.models.launch_candidate import LaunchCandidate
from backend.models.launch_signal import LaunchSignal

logger = logging.getLogger(__name__)


def _fmt_usd(v) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
        if f >= 1_000_000:
            return f"${f/1_000_000:,.2f}M"
        if f >= 1_000:
            return f"${f/1_000:,.1f}K"
        return f"${f:,.0f}"
    except Exception:
        return "—"


def _format_alert(
    cand: LaunchCandidate, signals: list[LaunchSignal], tier: str
) -> str:
    header = {"S": "🟥 S-TIER", "A": "🟧 A-TIER"}.get(tier, "🟨 Launch")
    dex_url = ""
    if cand.chain == "bsc":
        dex_url = f"https://bscscan.com/token/{cand.contract_address}"
    elif cand.chain == "solana":
        dex_url = f"https://solscan.io/token/{cand.contract_address}"

    # Collapse signals into a short bulleted list ordered by points
    unique = {}
    for s in signals:
        prev = unique.get(s.signal_type)
        if prev is None or s.confidence > prev.confidence:
            unique[s.signal_type] = s
    signals_sorted = sorted(
        unique.values(), key=lambda s: (-s.points, -s.confidence)
    )
    signal_lines = []
    for s in signals_sorted[:6]:
        desc = s.description or s.signal_type
        signal_lines.append(f"  • <b>{s.signal_type}</b> — {desc} ({s.confidence})")

    sym = cand.token_symbol or "(symbol pending)"
    name = cand.token_name or ""

    message = f"""{header} — LAUNCH DETECTED
Score: {cand.composite_score}/100  |  Chain: {cand.chain.upper()}

<b>🪙 {sym} {name}</b>
Contract: <code>{cand.contract_address}</code>
Deployer: <code>{(cand.deployer_address or '—')[:12]}</code>
Initial LP: {_fmt_usd(cand.initial_lp_usd)}

<b>SIGNALS CONVERGED:</b>
{chr(10).join(signal_lines) if signal_lines else '  (none)'}

🔗 <a href="{dex_url}">Explorer</a>

<i>⚠️ Entry: spot only | 2-5% bankroll | -15% stop loss</i>"""
    return message


async def fire_launch_alert(
    cand: LaunchCandidate, signals: list[LaunchSignal], tier: str
) -> None:
    """Send TG + log to Alert table. Respects MAX_ALERTS_PER_DAY."""
    db = SessionLocal()
    try:
        count_today = get_alerts_fired_today(db)
        if count_today >= settings.MAX_ALERTS_PER_DAY:
            logger.info(
                f"Daily launch alert cap reached ({count_today}/{settings.MAX_ALERTS_PER_DAY}) — "
                f"suppressing TG for {cand.contract_address}, still logging to DB"
            )
            alert = Alert(
                contract_address=cand.contract_address,
                token_symbol=cand.token_symbol,
                score_at_alert=cand.composite_score,
                alert_type=f"launch_{tier}",
                trigger_reason=(
                    f"Tier {tier} — signals: "
                    f"{list({s.signal_type for s in signals})}. "
                    "DAILY LIMIT — not sent to Telegram."
                ),
                telegram_sent=False,
                fired_at=datetime.utcnow(),
            )
            db.add(alert)
            db.commit()
            return

        message = _format_alert(cand, signals, tier)
        msg_id = await send_telegram_message(message)

        alert = Alert(
            contract_address=cand.contract_address,
            token_symbol=cand.token_symbol,
            score_at_alert=cand.composite_score,
            alert_type=f"launch_{tier}",
            trigger_reason=f"Tier {tier} launch; signals: {list({s.signal_type for s in signals})}",
            telegram_sent=msg_id is not None,
            telegram_message_id=msg_id,
            fired_at=datetime.utcnow(),
        )
        db.add(alert)
        db.commit()
        logger.info(
            f"Launch alert fired: {cand.contract_address} tier={tier} "
            f"score={cand.composite_score}"
        )
    except Exception as e:
        logger.error(f"fire_launch_alert error: {e}")
    finally:
        db.close()
