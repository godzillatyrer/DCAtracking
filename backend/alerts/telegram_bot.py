"""
Telegram Alert System — Push notifications for detected pump signals.

Sends formatted alerts to a Telegram channel/group when a token crosses
the alert threshold or when a known operator makes a notable move.
"""

import logging
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import SessionLocal
from backend.models.alert import Alert
from backend.models.flagged_token import FlaggedToken
from backend.models.watchlist import Watchlist
from backend.ai.briefing_generator import generate_briefing

logger = logging.getLogger(__name__)

TELEGRAM_API = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"


async def send_telegram_message(text: str) -> int | None:
    """
    Send a message to the configured Telegram chat.
    Returns the message ID on success, None on failure.
    """
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured — skipping alert")
        return None

    url = f"{TELEGRAM_API}/sendMessage"
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(url, json={
                "chat_id": settings.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    return data["result"]["message_id"]
            logger.error(f"Telegram API error: {resp.status_code} — {resp.text}")
        except Exception as e:
            logger.error(f"Error sending Telegram message: {e}")

    return None


def format_score_alert(token_data: dict, briefing: str, score: int) -> str:
    """Format a score-based alert message for Telegram."""
    confidence_icon = "\U0001f534 HIGH" if score >= 70 else "\U0001f7e1 MEDIUM"

    contract = token_data.get("contract_address", "N/A")
    short_contract = f"{contract[:6]}...{contract[-4:]}" if len(contract) > 10 else contract

    volume = token_data.get("volume_24h", 0)
    mcap = token_data.get("market_cap", 0)

    try:
        volume_str = f"${float(volume):,.0f}"
    except (ValueError, TypeError):
        volume_str = "N/A"

    try:
        mcap_str = f"${float(mcap):,.0f}"
    except (ValueError, TypeError):
        mcap_str = "N/A"

    message = f"""{confidence_icon} CONFIDENCE — Score: {score}/100

<b>{token_data.get('symbol', '?')} ({token_data.get('name', 'Unknown')})</b>
BSC | {short_contract}
Price: ${token_data.get('price_usd', 'N/A')}
24h Vol: {volume_str}
MCap: {mcap_str}
Float: {token_data.get('float_pct', 'N/A')}%
Top 10 Hold: {token_data.get('top_10_holder_pct', 'N/A')}%

<b>AI BRIEFING:</b>
{briefing}

<a href="{token_data.get('dex_url', '#')}">DEX Screener</a> | <a href="https://bscscan.com/token/{contract}">BscScan</a>

Entry Rules: Spot only, 2-5% bankroll, -15% stop loss"""

    return message


def format_wallet_alert(activity: dict) -> str:
    """Format a wallet tracker alert message for Telegram."""
    message = f"""\U0001f6a8 KNOWN OPERATOR ACTIVITY

<b>{activity.get('wallet_label', 'Unknown Operator')}</b>
Wallet: {activity['wallet'][:10]}...
Activity: {activity['activity']}
Token: {activity.get('token_symbol', 'BNB')}
Amount: {activity.get('amount', 'N/A')}
"""

    if activity.get('is_new_token'):
        message += "\n\U0001f525 <b>NEW TOKEN ACCUMULATION — HIGH PRIORITY</b>"

    return message


async def fire_score_alert(
    contract_address: str,
    score: int,
    breakdown: dict,
    db: Session | None = None,
):
    """
    Fire a score-based alert for a token crossing the threshold.
    Generates AI briefing and sends Telegram notification.
    """
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    try:
        # Get token data
        flagged = db.query(FlaggedToken).filter_by(contract_address=contract_address).first()
        if not flagged:
            return

        # Don't fire alerts for tokens with no actual data (auto-flagged stubs)
        if not flagged.token_name and not flagged.token_symbol:
            logger.info(f"Skipping alert for {contract_address[:10]} — no token data yet")
            return

        if not flagged.price_usd and not flagged.volume_24h:
            logger.info(f"Skipping alert for {flagged.token_symbol or contract_address[:10]} — no price/volume data")
            return

        watchlist = db.query(Watchlist).filter_by(contract_address=contract_address).first()
        if watchlist and watchlist.alert_fired:
            logger.info(f"Alert already fired for {flagged.token_symbol} — skipping")
            return

        # Build token data dict for briefing
        token_data = {
            "name": flagged.token_name,
            "symbol": flagged.token_symbol,
            "contract_address": contract_address,
            "price_usd": str(flagged.price_usd) if flagged.price_usd else "N/A",
            "volume_24h": str(flagged.volume_24h) if flagged.volume_24h else "N/A",
            "market_cap": str(flagged.market_cap) if flagged.market_cap else "N/A",
            "score": score,
            "dex_url": flagged.dex_url or "",
        }

        # Add profile data if available
        if watchlist:
            token_data.update({
                "float_pct": str(watchlist.score_breakdown.get("float_pct", "N/A")) if watchlist.score_breakdown else "N/A",
                "top_10_holder_pct": "N/A",
                "cluster_detected": watchlist.cluster_detected,
                "cluster_wallet_count": watchlist.cluster_wallet_count,
                "exchange_deposits_detected": watchlist.exchange_deposits_detected,
            })

        # Generate AI briefing
        briefing = generate_briefing(token_data)

        # Format and send Telegram message
        message = format_score_alert(token_data, briefing, score)
        msg_id = await send_telegram_message(message)

        # Log alert to database
        alert = Alert(
            contract_address=contract_address,
            token_symbol=flagged.token_symbol,
            score_at_alert=score,
            alert_type="score_threshold",
            trigger_reason=f"Score {score} >= threshold {settings.ALERT_THRESHOLD}. Signals: {list(breakdown.keys())}",
            ai_briefing=briefing,
            price_at_alert=flagged.price_usd,
            market_cap_at_alert=flagged.market_cap,
            telegram_sent=msg_id is not None,
            telegram_message_id=msg_id,
            fired_at=datetime.utcnow(),
        )
        db.add(alert)

        # Mark alert as fired in watchlist
        if watchlist:
            watchlist.alert_fired = True
            watchlist.alert_fired_at = datetime.utcnow()
            watchlist.price_at_alert = flagged.price_usd
            watchlist.ai_briefing = briefing

        db.commit()
        logger.info(f"Alert fired for {flagged.token_symbol} (score={score})")

    except Exception as e:
        logger.error(f"Error firing alert for {contract_address}: {e}")
    finally:
        if close_db:
            db.close()


async def fire_wallet_alert(activity: dict):
    """Fire an alert for notable wallet tracker activity."""
    message = format_wallet_alert(activity)
    msg_id = await send_telegram_message(message)

    if msg_id:
        logger.info(f"Wallet alert sent for {activity.get('wallet_label', 'unknown')}")

    db = SessionLocal()
    try:
        alert = Alert(
            contract_address=activity.get("token_contract", ""),
            token_symbol=activity.get("token_symbol", ""),
            alert_type="wallet_tracker",
            trigger_reason=f"Known operator {activity.get('wallet_label', '')} — {activity['activity']}",
            telegram_sent=msg_id is not None,
            telegram_message_id=msg_id,
            fired_at=datetime.utcnow(),
        )
        db.add(alert)
        db.commit()
    finally:
        db.close()
