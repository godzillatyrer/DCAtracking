"""Telegram alerts formatted for leveraged trading decisions."""

import logging

from telegram import Bot
from telegram.constants import ParseMode

import config
import database
from analyzer import Signal

logger = logging.getLogger(__name__)


def _format_usd(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.2f}"


def _format_duration(hours: float) -> str:
    if hours >= 24:
        days = hours / 24
        return f"{days:.1f}d"
    return f"{hours:.1f}h"


def format_alert_message(signal: Signal, conviction: float,
                         token_data: dict | None) -> str:
    """Format alert for a leveraged long entry decision."""
    d = signal.details

    if signal.signal_type == "mega_dca":
        return _format_mega_dca(signal, conviction, token_data)
    elif signal.signal_type == "coordinated_accumulation":
        return _format_coordinated(signal, conviction, token_data)

    return f"Signal: {signal.signal_type} on {signal.token_mint[:16]}"


def _format_mega_dca(signal: Signal, conviction: float,
                     token_data: dict | None) -> str:
    d = signal.details
    sev = "\U0001f534" if signal.severity == "critical" else "\U0001f7e0"
    value = d.get("dca_value_usd", 0)

    lines = [
        f"{sev} <b>MASSIVE DCA ORDER</b> {sev}",
        "",
    ]

    # Token
    symbol = token_data.get("symbol", "???") if token_data else "???"
    name = token_data.get("name", "") if token_data else ""
    price = token_data.get("price_usd", 0) if token_data else 0
    lines.append(f"<b>{symbol}</b> ({name})")
    lines.append(f"Price: ${price:.8f}" if price < 0.01 else f"Price: ${price:.4f}")
    lines.append("")

    # The order
    lines.append(f"DCA Size: <b>{_format_usd(value)}</b>")
    lines.append(f"MCap: {_format_usd(d.get('mcap', 0))}")
    lines.append(f"% of MCap: <b>{d.get('pct_of_mcap', 0):.2f}%</b>")
    vol = d.get("volume_24h", 0)
    if vol > 0:
        lines.append(f"vs 24h Vol: <b>{d.get('pct_of_daily_volume', 0):.0f}%</b>")
    else:
        lines.append("24h Volume: <b>$0 (dead volume)</b>")
    lines.append(f"Liquidity: {_format_usd(d.get('liquidity_usd', 0))}")
    lines.append("")

    # Buy pressure duration
    duration = d.get("duration_hours", 0)
    cycles = d.get("total_cycles", 0)
    per_cycle = d.get("per_cycle_usd", 0)
    freq_secs = d.get("cycle_frequency_seconds", 0)

    if duration > 0 and cycles > 0:
        if freq_secs >= 3600:
            freq_str = f"every {freq_secs / 3600:.0f}h"
        elif freq_secs >= 60:
            freq_str = f"every {freq_secs / 60:.0f}m"
        else:
            freq_str = f"every {freq_secs}s"

        lines.append(f"Buy Pressure: <b>{_format_duration(duration)}</b>")
        lines.append(f"  {cycles} cycles, {_format_usd(per_cycle)} {freq_str}")
    lines.append("")

    # Conviction
    lines.append(f"Conviction: <b>{conviction:.0f}/100</b>")

    # Wallet
    wallet = d.get("user_wallet", "")
    if wallet:
        lines.append(f"Wallet: <code>{wallet}</code>")

    # Links + CA
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


def _format_coordinated(signal: Signal, conviction: float,
                        token_data: dict | None) -> str:
    d = signal.details
    sev = "\U0001f534" if signal.severity == "critical" else "\U0001f7e0"

    lines = [
        f"{sev} <b>COORDINATED DCA ACCUMULATION</b> {sev}",
        "",
    ]

    symbol = token_data.get("symbol", "???") if token_data else "???"
    name = token_data.get("name", "") if token_data else ""
    price = token_data.get("price_usd", 0) if token_data else 0
    lines.append(f"<b>{symbol}</b> ({name})")
    lines.append(f"Price: ${price:.8f}" if price < 0.01 else f"Price: ${price:.4f}")
    lines.append("")

    lines.append(f"Total DCA: <b>{_format_usd(d.get('total_dca_value_usd', 0))}</b>")
    lines.append(f"Wallets: <b>{d.get('unique_wallets', 0)}</b>")
    lines.append(f"Orders: <b>{d.get('order_count', 0)}</b> (last {d.get('window_hours', 24)}h)")
    lines.append(f"MCap: {_format_usd(d.get('mcap', 0))}")
    lines.append(f"% of MCap: <b>{d.get('pct_of_mcap', 0):.2f}%</b>")
    vol = d.get("volume_24h", 0)
    if vol:
        lines.append(f"24h Volume: {_format_usd(vol)}")
    lines.append(f"Liquidity: {_format_usd(d.get('liquidity_usd', 0))}")
    lines.append("")

    lines.append(f"Conviction: <b>{conviction:.0f}/100</b>")
    lines.append("<i>Multiple wallets accumulating — possible insider coordination</i>")

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


async def send_alert(signal: Signal, conviction: float,
                     token_data: dict | None) -> bool:
    """Send alert via Telegram if not recently sent."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured, logging alert to console")
        msg = format_alert_message(signal, conviction, token_data)
        logger.info("ALERT:\n%s", msg)
        return True

    if database.was_alert_sent_recently(
        signal.token_mint, signal.signal_type,
        cooldown_hours=config.ALERT_COOLDOWN_HOURS,
    ):
        logger.debug(
            "Skipping %s/%s - cooldown",
            signal.token_mint[:16], signal.signal_type,
        )
        return False

    msg = format_alert_message(signal, conviction, token_data)

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
            "Alert sent: %s - %s [conviction: %.0f]",
            signal.token_mint[:16], signal.signal_type, conviction,
        )
        return True
    except Exception as e:
        logger.error("Failed to send Telegram alert: %s", e)
        return False
