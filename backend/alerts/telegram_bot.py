"""
Telegram Alert System — Curated, high-quality notifications.

Design principles:
- MAX 5 alerts per day (hard cap)
- Only alert when token has COMPLETE data (name, price, volume, holders, score)
- Each alert includes a full AI briefing with all metadata compiled
- Daily summary digest at end of day
- No spam, no stubs, no N/A data
"""

import logging
from datetime import datetime, timedelta

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import SessionLocal
from backend.models.alert import Alert
from backend.models.flagged_token import FlaggedToken
from backend.models.token_profile import TokenProfile
from backend.models.watchlist import Watchlist
from backend.ai.briefing_generator import generate_briefing

logger = logging.getLogger(__name__)


def _telegram_api_url():
    return f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"


async def send_telegram_message(text: str) -> int | None:
    """Send a message to the configured Telegram chat."""
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured — skipping alert")
        return None

    url = f"{_telegram_api_url()}/sendMessage"
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


def get_alerts_fired_today(db: Session) -> int:
    """Count how many alerts were sent via Telegram today."""
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return db.query(func.count(Alert.id)).filter(
        Alert.fired_at >= today_start,
        Alert.telegram_sent.is_(True),
    ).scalar() or 0


def has_complete_data(flagged: FlaggedToken) -> bool:
    """Check if a token has enough data to generate a quality alert."""
    if not flagged.token_name and not flagged.token_symbol:
        return False
    if not flagged.price_usd:
        return False
    if not flagged.volume_24h:
        return False
    return True


def format_full_alert(
    flagged: FlaggedToken,
    profile: TokenProfile | None,
    watchlist: Watchlist | None,
    briefing: str,
    score: int,
    breakdown: dict,
) -> str:
    """Format a comprehensive alert with all available data."""
    confidence = "\U0001f534 HIGH" if score >= 80 else "\U0001f7e0 HIGH" if score >= 70 else "\U0001f7e1 MEDIUM"

    contract = flagged.contract_address
    short_contract = f"{contract[:6]}...{contract[-4:]}"

    def fmt_usd(val):
        if not val:
            return "N/A"
        try:
            v = float(val)
            if v >= 1_000_000:
                return f"${v / 1_000_000:,.2f}M"
            if v >= 1_000:
                return f"${v / 1_000:,.1f}K"
            return f"${v:,.2f}"
        except (ValueError, TypeError):
            return "N/A"

    # Build signal summary
    signals = []
    for signal, points in sorted(breakdown.items(), key=lambda x: -x[1]):
        signals.append(f"  +{points} {signal.replace('_', ' ')}")
    signal_text = "\n".join(signals[:8])  # Top 8 signals

    # Profile data
    float_pct = f"{profile.float_pct}%" if profile and profile.float_pct else "N/A"
    top10 = f"{profile.top_10_holder_pct}%" if profile and profile.top_10_holder_pct else "N/A"
    age = f"{profile.token_age_days}d" if profile and profile.token_age_days else "N/A"
    verified = "Yes" if profile and profile.is_contract_verified else "No" if profile else "N/A"
    narrative = ", ".join(profile.narrative_tags) if profile and profile.narrative_tags else "N/A"

    # Cluster/flow data
    cluster = "None"
    if watchlist and watchlist.cluster_detected:
        cluster = f"YES — {watchlist.cluster_wallet_count} wallets"
    exchange_flow = "None"
    if watchlist and watchlist.exchange_deposits_detected:
        exchange_flow = f"DETECTED — {fmt_usd(watchlist.exchange_deposit_volume)}"

    message = f"""{confidence} — Score: {score}/100

<b>\U0001fa99 {flagged.token_symbol} ({flagged.token_name})</b>
\U0001f4cd BSC | {short_contract}

<b>MARKET DATA:</b>
\U0001f4b0 Price: ${flagged.price_usd}
\U0001f4ca 24h Volume: {fmt_usd(flagged.volume_24h)}
\U0001f3e6 Market Cap: {fmt_usd(flagged.market_cap)}
\U0001f4a7 Liquidity: {fmt_usd(flagged.liquidity_usd)}

<b>TOKEN PROFILE:</b>
\U0001f4c9 Float: {float_pct}
\U0001f40b Top 10 Holders: {top10}
\U0001f4c5 Token Age: {age}
\u2705 Contract Verified: {verified}
\U0001f3f7 Narrative: {narrative}

<b>ON-CHAIN SIGNALS:</b>
\U0001f50d Wallet Cluster: {cluster}
\U0001f4e4 Exchange Flow: {exchange_flow}

<b>SCORE BREAKDOWN:</b>
<pre>{signal_text}</pre>

<b>\U0001f916 AI ANALYSIS:</b>
{briefing}

\U0001f517 <a href="{flagged.dex_url or '#'}">DEX Screener</a> | <a href="https://bscscan.com/token/{contract}">BscScan</a>

<i>\u26a0\ufe0f Spot only | 2-5% bankroll | -15% stop loss</i>"""

    return message


async def fire_score_alert(
    contract_address: str,
    score: int,
    breakdown: dict,
    db: Session | None = None,
):
    """
    Fire a high-quality alert. Requirements:
    1. Token must have complete data (name, price, volume)
    2. Alert not already fired for this token
    3. Daily limit not exceeded (max 5)
    4. AI briefing generated with full context
    """
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    try:
        flagged = db.query(FlaggedToken).filter_by(contract_address=contract_address).first()
        if not flagged:
            return

        # Gate 1: Must have complete data
        if not has_complete_data(flagged):
            logger.info(f"Skipping alert for {contract_address[:10]} — incomplete data")
            return

        # Gate 2: Not already alerted
        watchlist = db.query(Watchlist).filter_by(contract_address=contract_address).first()
        if watchlist and watchlist.alert_fired:
            return

        # Gate 3: Daily limit
        alerts_today = get_alerts_fired_today(db)
        if alerts_today >= settings.MAX_ALERTS_PER_DAY:
            logger.info(
                f"Daily alert limit reached ({alerts_today}/{settings.MAX_ALERTS_PER_DAY}) "
                f"— skipping {flagged.token_symbol}. Will log to DB without Telegram."
            )
            # Still log to database, just don't send Telegram
            alert = Alert(
                contract_address=contract_address,
                token_symbol=flagged.token_symbol,
                score_at_alert=score,
                alert_type="score_threshold",
                trigger_reason=f"Score {score} >= {settings.ALERT_THRESHOLD}. Signals: {list(breakdown.keys())}. DAILY LIMIT — not sent to Telegram.",
                price_at_alert=flagged.price_usd,
                market_cap_at_alert=flagged.market_cap,
                telegram_sent=False,
                fired_at=datetime.utcnow(),
            )
            db.add(alert)
            if watchlist:
                watchlist.alert_fired = True
                watchlist.alert_fired_at = datetime.utcnow()
            db.commit()
            return

        # Get profile for enriched data
        profile = db.query(TokenProfile).filter_by(contract_address=contract_address).first()

        # Build comprehensive token data for AI briefing
        token_data = {
            "name": flagged.token_name,
            "symbol": flagged.token_symbol,
            "contract_address": contract_address,
            "price_usd": str(flagged.price_usd) if flagged.price_usd else "N/A",
            "volume_24h": str(flagged.volume_24h) if flagged.volume_24h else "N/A",
            "market_cap": str(flagged.market_cap) if flagged.market_cap else "N/A",
            "score": score,
            "dex_url": flagged.dex_url or "",
            "float_pct": str(profile.float_pct) if profile and profile.float_pct else "N/A",
            "top_10_holder_pct": str(profile.top_10_holder_pct) if profile and profile.top_10_holder_pct else "N/A",
            "token_age_days": profile.token_age_days if profile else "N/A",
            "binance_alpha": profile.binance_alpha if profile else False,
            "cluster_detected": watchlist.cluster_detected if watchlist else False,
            "cluster_wallet_count": watchlist.cluster_wallet_count if watchlist else 0,
            "exchange_deposits_detected": watchlist.exchange_deposits_detected if watchlist else False,
        }

        # Generate AI briefing
        briefing = generate_briefing(token_data)

        # Format comprehensive message
        message = format_full_alert(flagged, profile, watchlist, briefing, score, breakdown)

        # Send to Telegram
        msg_id = await send_telegram_message(message)

        # Log to database
        alert = Alert(
            contract_address=contract_address,
            token_symbol=flagged.token_symbol,
            score_at_alert=score,
            alert_type="score_threshold",
            trigger_reason=f"Score {score} >= {settings.ALERT_THRESHOLD}. Signals: {list(breakdown.keys())}",
            ai_briefing=briefing,
            price_at_alert=flagged.price_usd,
            market_cap_at_alert=flagged.market_cap,
            telegram_sent=msg_id is not None,
            telegram_message_id=msg_id,
            fired_at=datetime.utcnow(),
        )
        db.add(alert)

        if watchlist:
            watchlist.alert_fired = True
            watchlist.alert_fired_at = datetime.utcnow()
            watchlist.price_at_alert = flagged.price_usd
            watchlist.ai_briefing = briefing

        db.commit()
        logger.info(f"Alert #{alerts_today + 1}/{settings.MAX_ALERTS_PER_DAY} fired for {flagged.token_symbol} (score={score})")

    except Exception as e:
        logger.error(f"Error firing alert for {contract_address}: {e}")
    finally:
        if close_db:
            db.close()


async def send_daily_digest():
    """
    Send a daily summary of all scanner activity.
    Called once per day by the scheduler.
    """
    db = SessionLocal()
    try:
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        # Today's stats
        alerts_today = db.query(func.count(Alert.id)).filter(
            Alert.fired_at >= today_start
        ).scalar() or 0

        alerts_sent = db.query(func.count(Alert.id)).filter(
            Alert.fired_at >= today_start,
            Alert.telegram_sent.is_(True),
        ).scalar() or 0

        new_flags = db.query(func.count(FlaggedToken.id)).filter(
            FlaggedToken.first_flagged_at >= today_start,
        ).scalar() or 0

        watchlist_count = db.query(func.count(Watchlist.id)).scalar() or 0

        high_score_tokens = db.query(Watchlist).filter(
            Watchlist.current_score >= 50,
            Watchlist.alert_fired.is_(False),
        ).order_by(Watchlist.current_score.desc()).limit(5).all()

        # Build digest
        message = f"""\U0001f4ca <b>DAILY SCANNER DIGEST</b>
{datetime.utcnow().strftime('%Y-%m-%d')}

<b>TODAY'S ACTIVITY:</b>
\U0001f6a8 Alerts fired: {alerts_sent} / {settings.MAX_ALERTS_PER_DAY} max
\U0001f6a9 New tokens flagged: {new_flags}
\U0001f440 Watchlist size: {watchlist_count}
"""

        if high_score_tokens:
            message += "\n<b>TOP WATCHLIST (not yet alerted):</b>\n"
            for w in high_score_tokens:
                flagged = db.query(FlaggedToken).filter_by(
                    contract_address=w.contract_address
                ).first()
                name = flagged.token_symbol if flagged and flagged.token_symbol else w.contract_address[:10]
                message += f"  {w.current_score}/100 — {name}\n"

        if alerts_today == 0 and new_flags == 0:
            message += "\n<i>Quiet day — no significant signals detected.</i>"

        await send_telegram_message(message)
        logger.info("Daily digest sent")

    except Exception as e:
        logger.error(f"Error sending daily digest: {e}")
    finally:
        db.close()
