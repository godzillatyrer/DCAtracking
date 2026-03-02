"""Telegram alert system for DCA order detection."""

import logging

from telegram import Bot
from telegram.constants import ParseMode

import config
import database
from analyzer import Signal

logger = logging.getLogger(__name__)

SIGNAL_EMOJI = {
    "large_dca": "\U0001f4b0",        # money bag
    "dca_cluster": "\U0001f6a8",       # rotating light
    "dca_on_dead_token": "\U0001f480",  # skull
    "whale_dca": "\U0001f40b",         # whale
}

SEVERITY_EMOJI = {
    "low": "\U0001f7e2",       # green circle
    "medium": "\U0001f7e1",    # yellow circle
    "high": "\U0001f7e0",      # orange circle
    "critical": "\U0001f534",  # red circle
}


def _format_usd(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.2f}"


def _signal_title(signal_type: str) -> str:
    titles = {
        "large_dca": "LARGE DCA ORDER DETECTED",
        "dca_cluster": "DCA ORDER CLUSTER",
        "dca_on_dead_token": "DCA ON DEAD TOKEN",
        "whale_dca": "WHALE DCA ORDER",
    }
    return titles.get(signal_type, signal_type.upper())


def format_alert_message(signal: Signal, crime_score: float,
                         token_data: dict | None) -> str:
    """Format a DCA signal into a readable Telegram message."""
    sig_emoji = SIGNAL_EMOJI.get(signal.signal_type, "\U00002753")
    sev_emoji = SEVERITY_EMOJI.get(signal.severity, "\U000026aa")

    lines = [
        f"{sig_emoji} <b>{_signal_title(signal.signal_type)}</b> {sev_emoji}",
        "",
    ]

    # Token info
    symbol = token_data.get("symbol", "???") if token_data else "???"
    name = token_data.get("name", "") if token_data else ""
    lines.append(f"<b>{symbol}</b> ({name})")
    lines.append("Chain: Solana")
    lines.append(f"Suspicion Score: <b>{crime_score:.0f}/100</b>")
    lines.append("")

    # Price info
    if token_data:
        price = token_data.get("price_usd", 0)
        mcap = token_data.get("market_cap", 0)
        vol = token_data.get("volume_24h", 0)
        liq = token_data.get("liquidity_usd", 0)

        lines.append(f"Price: ${price:.8f}" if price < 0.01 else f"Price: ${price:.4f}")
        if mcap:
            lines.append(f"Market Cap: {_format_usd(mcap)}")
        lines.append(f"24h Volume: {_format_usd(vol)}")
        if liq:
            lines.append(f"Liquidity: {_format_usd(liq)}")
        lines.append("")

    # Signal-specific details
    d = signal.details
    if signal.signal_type == "large_dca":
        lines.append(f"DCA Value: <b>{_format_usd(d.get('dca_value_usd', 0))}</b>")
        size_vs = d.get('size_vs_volume', 0)
        if size_vs < 999:
            lines.append(f"vs 24h Volume: <b>{size_vs:.1f}x</b>")
        else:
            lines.append("vs 24h Volume: <b>token has ZERO volume</b>")
        cycles = d.get('total_cycles', 0)
        freq = d.get('cycle_frequency_hours', 0)
        if cycles:
            lines.append(f"Cycles: {cycles}")
        if freq:
            lines.append(f"Frequency: every {freq:.1f}h")
        wallet = d.get('user_wallet', '')
        if wallet:
            lines.append(f"Wallet: <code>{wallet[:8]}...{wallet[-4:]}</code>")

    elif signal.signal_type == "dca_cluster":
        window = d.get('window_hours', 24)
        lines.append(f"Orders (last {window}h): <b>{d.get('order_count', 0)}</b>")
        lines.append(f"Total DCA Value: <b>{_format_usd(d.get('total_value_usd', 0))}</b>")
        lines.append(f"Unique Wallets: <b>{d.get('unique_wallets', 0)}</b>")
        vol = d.get('volume_24h', 0)
        if vol:
            lines.append(f"vs 24h Volume: {_format_usd(vol)}")

    elif signal.signal_type == "dca_on_dead_token":
        lines.append(f"DCA Value: <b>{_format_usd(d.get('dca_value_usd', 0))}</b>")
        lines.append(f"Avg Daily Volume: {_format_usd(d.get('avg_daily_volume', 0))}")
        lines.append(f"Revival Ratio: <b>{d.get('revival_ratio', 0):.1f}x</b>")
        lines.append("<i>Token was essentially dead - now being DCA'd into</i>")

    elif signal.signal_type == "whale_dca":
        lines.append(f"DCA Value: <b>{_format_usd(d.get('dca_value_usd', 0))}</b>")
        cycles = d.get('total_cycles', 0)
        freq = d.get('cycle_frequency_hours', 0)
        if cycles:
            lines.append(f"Cycles: {cycles}")
        if freq:
            lines.append(f"Frequency: every {freq:.1f}h")
        wallet = d.get('user_wallet', '')
        if wallet:
            lines.append(f"Wallet: <code>{wallet[:8]}...{wallet[-4:]}</code>")
        if d.get('token_mcap'):
            lines.append(f"Token MCap: {_format_usd(d['token_mcap'])}")

    # Links
    mint = signal.token_mint
    lines.append("")
    lines.append(
        f'<a href="https://birdeye.so/token/{mint}?chain=solana">Birdeye</a> | '
        f'<a href="https://solscan.io/token/{mint}">Solscan</a> | '
        f'<a href="https://dexscreener.com/solana/{mint}">DexScreener</a>'
    )
    lines.append("")
    lines.append(f"<code>{mint}</code>")

    return "\n".join(lines)


async def send_alert(signal: Signal, crime_score: float,
                     token_data: dict | None) -> bool:
    """Send alert via Telegram if not recently sent."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured, logging alert to console")
        msg = format_alert_message(signal, crime_score, token_data)
        logger.info("ALERT:\n%s", msg)
        return True

    # Check cooldown
    if database.was_alert_sent_recently(
        signal.token_mint, signal.signal_type,
        cooldown_hours=config.ALERT_COOLDOWN_HOURS,
    ):
        logger.debug(
            "Skipping alert for %s/%s - cooldown active",
            signal.token_mint[:16], signal.signal_type,
        )
        return False

    msg = format_alert_message(signal, crime_score, token_data)

    try:
        bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=msg,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        database.record_alert(signal.token_mint, signal.signal_type, msg)
        logger.info(
            "Alert sent: %s - %s [score: %.0f]",
            signal.token_mint[:16], signal.signal_type, crime_score,
        )
        return True
    except Exception as e:
        logger.error("Failed to send Telegram alert: %s", e)
        return False
