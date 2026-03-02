"""Telegram alert system for sending notifications about suspicious tokens."""

import logging

from telegram import Bot
from telegram.constants import ParseMode

import config
import database
from analyzer import Signal

logger = logging.getLogger(__name__)

SIGNAL_EMOJI = {
    "volume_spike": "\U0001f4c8",     # chart increasing
    "buy_pressure": "\U0001f6a8",     # rotating light
    "dead_revival": "\U0001f480",     # skull (back from dead)
    "accumulation": "\U0001f575",     # detective
}

SEVERITY_EMOJI = {
    "low": "\U0001f7e2",       # green circle
    "medium": "\U0001f7e1",    # yellow circle
    "high": "\U0001f7e0",      # orange circle
    "critical": "\U0001f534",  # red circle
}

CHAIN_NAMES = {
    "solana": "Solana",
    "ethereum": "Ethereum",
    "bsc": "BSC",
}


def _format_usd(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.2f}"


def _format_number(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:.0f}"


def format_signal_message(signal: Signal, crime_score: float,
                          token_data: dict) -> str:
    """Format a signal into a readable Telegram message."""

    sig_emoji = SIGNAL_EMOJI.get(signal.signal_type, "\U00002753")
    sev_emoji = SEVERITY_EMOJI.get(signal.severity, "\U000026aa")
    chain_name = CHAIN_NAMES.get(signal.chain, signal.chain.upper())

    # Header
    lines = [
        f"{sig_emoji} <b>{_signal_title(signal.signal_type)}</b> {sev_emoji}",
        "",
        f"<b>{signal.symbol}</b> ({signal.name})",
        f"Chain: {chain_name}",
        f"Crime Score: <b>{crime_score:.0f}/100</b> {'🔥' if crime_score >= 60 else ''}",
        "",
    ]

    # Price info
    price = token_data.get("price_usd", 0)
    mcap = token_data.get("market_cap", 0)
    liq = token_data.get("liquidity_usd", 0)
    lines.append(f"Price: ${price:.8f}" if price < 0.01 else f"Price: ${price:.4f}")
    lines.append(f"Market Cap: {_format_usd(mcap)}")
    lines.append(f"Liquidity: {_format_usd(liq)}")
    lines.append("")

    # Signal-specific details
    details = signal.details
    if signal.signal_type == "volume_spike":
        lines.append(f"1h Volume: {_format_usd(details.get('current_1h_volume', 0))}")
        lines.append(f"Avg 1h Volume: {_format_usd(details.get('avg_1h_volume', 0))}")
        lines.append(f"Spike: <b>{details.get('volume_ratio', 0)}x</b> normal")

    elif signal.signal_type == "buy_pressure":
        lines.append(f"Buy Ratio (1h): <b>{details.get('buy_ratio_1h', 0):.0%}</b>")
        lines.append(f"Buy Ratio (6h): <b>{details.get('buy_ratio_6h', 0):.0%}</b>")
        lines.append(
            f"Buys/Sells (1h): {details.get('buys_1h', 0)}/{details.get('sells_1h', 0)}"
        )
        lines.append(
            f"Buys/Sells (6h): {details.get('buys_6h', 0)}/{details.get('sells_6h', 0)}"
        )

    elif signal.signal_type == "dead_revival":
        lines.append(
            f"Previous Avg Volume: {_format_usd(details.get('prev_avg_volume', 0))}"
        )
        lines.append(f"Current 1h Volume: {_format_usd(details.get('current_1h_volume', 0))}")
        lines.append(f"Revival: <b>{details.get('revival_ratio', 0)}x</b> increase")

    elif signal.signal_type == "accumulation":
        lines.append(
            f"Buy Spike: <b>{details.get('buy_spike_ratio', 0)}x</b> normal buys"
        )
        lines.append(f"Price Change (1h): {details.get('price_change_1h', 0):+.1f}%")
        lines.append(f"1h Volume: {_format_usd(details.get('volume_1h', 0))}")
        lines.append("Stealth accumulation - price hasn't moved yet")

    lines.append("")

    # Volume & transaction summary
    lines.append(
        f"24h Vol: {_format_usd(token_data.get('volume_24h', 0))} | "
        f"Buys: {_format_number(token_data.get('buys_24h', 0))} | "
        f"Sells: {_format_number(token_data.get('sells_24h', 0))}"
    )

    # Price changes
    lines.append(
        f"Change: 1h {token_data.get('price_change_1h', 0):+.1f}% | "
        f"6h {token_data.get('price_change_6h', 0):+.1f}% | "
        f"24h {token_data.get('price_change_24h', 0):+.1f}%"
    )

    # DexScreener link
    dex_url = token_data.get("dex_url", "")
    if dex_url:
        lines.append("")
        lines.append(f'<a href="{dex_url}">View on DexScreener</a>')

    # Token address (for easy copy)
    lines.append("")
    lines.append(f"<code>{signal.token_address}</code>")

    return "\n".join(lines)


def _signal_title(signal_type: str) -> str:
    titles = {
        "volume_spike": "VOLUME SPIKE DETECTED",
        "buy_pressure": "HEAVY BUY PRESSURE",
        "dead_revival": "DEAD TOKEN REVIVAL",
        "accumulation": "STEALTH ACCUMULATION",
    }
    return titles.get(signal_type, signal_type.upper())


async def send_alert(signal: Signal, crime_score: float,
                     token_data: dict) -> bool:
    """Send alert via Telegram if not recently sent."""

    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured, logging alert to console")
        msg = format_signal_message(signal, crime_score, token_data)
        logger.info("ALERT:\n%s", msg)
        return False

    # Check cooldown to avoid spam
    if database.was_alert_sent_recently(
        signal.token_address, signal.chain, signal.signal_type
    ):
        logger.debug(
            "Skipping alert for %s/%s - already sent recently",
            signal.symbol, signal.signal_type,
        )
        return False

    msg = format_signal_message(signal, crime_score, token_data)

    try:
        bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=msg,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        database.record_alert(
            signal.token_address, signal.chain, signal.signal_type, msg
        )
        logger.info(
            "Alert sent for %s (%s) - %s [score: %.0f]",
            signal.symbol, signal.chain, signal.signal_type, crime_score,
        )
        return True
    except Exception as e:
        logger.error("Failed to send Telegram alert: %s", e)
        return False
