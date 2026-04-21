"""
Telegram alert delivery.

Minimal surface: send a plain text message, with a daily cap. The cabal
tracker uses this when multiple tracked wallets converge on the same
new token.
"""

import logging
from datetime import datetime

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import SessionLocal
from backend.models.alert import Alert

logger = logging.getLogger(__name__)


async def send_telegram_message(text: str) -> int | None:
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured — skipping message")
        return None
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(url, json={
                "chat_id": settings.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            if resp.status_code == 200 and resp.json().get("ok"):
                return resp.json()["result"]["message_id"]
            logger.error(f"Telegram API error: {resp.status_code} — {resp.text}")
        except Exception as e:
            logger.error(f"Telegram error: {e}")
    return None


def get_alerts_fired_today(db: Session) -> int:
    start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return db.query(func.count(Alert.id)).filter(
        Alert.fired_at >= start,
        Alert.telegram_sent.is_(True),
    ).scalar() or 0


async def fire_cabal_alert(
    mint: str,
    symbol: str,
    buyer_wallets: list[str],
    trigger_reason: str,
    db: Session | None = None,
) -> None:
    """Fire an alert when multiple tracked cabal wallets buy the same mint."""
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    try:
        alerts_today = get_alerts_fired_today(db)
        over_cap = alerts_today >= settings.MAX_ALERTS_PER_DAY

        short_mint = f"{mint[:6]}...{mint[-4:]}"
        wallets_txt = "\n".join(
            f"  • <code>{w[:8]}...{w[-4:]}</code>" for w in buyer_wallets[:10]
        )
        message = (
            f"🚨 <b>Cabal convergence</b>: {symbol or short_mint}\n\n"
            f"<b>Mint:</b> <code>{mint}</code>\n"
            f"<b>Tracked buyers ({len(buyer_wallets)}):</b>\n{wallets_txt}\n\n"
            f"<b>Trigger:</b> {trigger_reason}\n\n"
            f'<a href="https://dexscreener.com/solana/{mint}">DEX Screener</a> | '
            f'<a href="https://gmgn.ai/sol/token/{mint}">GMGN</a>'
        )

        msg_id = None
        if not over_cap:
            msg_id = await send_telegram_message(message)
        else:
            logger.info(
                f"Daily alert cap reached ({alerts_today}/{settings.MAX_ALERTS_PER_DAY})"
                f" — logging {symbol or short_mint} without sending."
            )

        alert = Alert(
            contract_address=mint,
            token_symbol=symbol or None,
            alert_type="cabal_convergence",
            trigger_reason=trigger_reason,
            telegram_sent=msg_id is not None,
            telegram_message_id=msg_id,
            fired_at=datetime.utcnow(),
        )
        db.add(alert)
        db.commit()
    except Exception as e:
        logger.error(f"fire_cabal_alert error: {e}")
    finally:
        if close_db:
            db.close()
