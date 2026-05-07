"""
Telegram alert delivery.

`fire_cabal_alert` is the single entry point. Its callers (alert
dispatcher) supply an already-gated, already-deduped event — this
function's only jobs are:
  1. Record the Alert row FIRST (so dedup holds even if Telegram
     fails), then
  2. Send the formatted message.

The message is rich HTML: name, symbol, CA (tap to copy), market
snapshot, the wallets that triggered, and action links.
"""

import logging
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import SessionLocal
from backend.models.alert import Alert

logger = logging.getLogger(__name__)


async def _send_raw(text: str, chat_id: str | None = None) -> int | None:
    """Actually POST a message to Telegram. No pause check, no
    rate-limit logic — used for both watcher alerts and bot command
    replies."""
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.warning("Telegram not configured — skipping message")
        return None
    target_chat = chat_id or settings.TELEGRAM_CHAT_ID
    if not target_chat:
        logger.warning("No Telegram chat target — skipping message")
        return None
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(url, json={
                "chat_id": target_chat,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            if resp.status_code == 200 and resp.json().get("ok"):
                return resp.json()["result"]["message_id"]
            logger.error(f"Telegram API error: {resp.status_code} — {resp.text[:300]}")
        except Exception as e:
            logger.error(f"Telegram error: {e}")
    return None


async def send_telegram_message(text: str) -> int | None:
    """Watcher alerts go through here. Honours the ALERTS_PAUSED
    master switch — when paused, returns None and the alert row will
    have telegram_sent=False (dedup still updates so we don't re-fire
    on resume)."""
    from backend import settings_cache
    if settings_cache.get("ALERTS_PAUSED", False):
        logger.info("Alerts paused via ALERTS_PAUSED — suppressing send")
        return None
    return await _send_raw(text)


async def send_command_reply(text: str, chat_id: str | None = None) -> int | None:
    """Bot replies — always sent regardless of pause state."""
    return await _send_raw(text, chat_id=chat_id)


async def send_with_keyboard(
    text: str,
    keyboard: list[list[dict]],
    chat_id: str | None = None,
    force_reply: bool = False,
) -> int | None:
    """Send a message with an inline keyboard. Used by the bot's
    button-driven menus."""
    if not settings.TELEGRAM_BOT_TOKEN:
        return None
    target_chat = chat_id or settings.TELEGRAM_CHAT_ID
    if not target_chat:
        return None
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    body = {
        "chat_id": target_chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if force_reply:
        body["reply_markup"] = {"force_reply": True, "selective": True}
    elif keyboard:
        body["reply_markup"] = {"inline_keyboard": keyboard}
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(url, json=body)
            if resp.status_code == 200 and resp.json().get("ok"):
                return resp.json()["result"]["message_id"]
            logger.error(f"send_with_keyboard error: {resp.status_code} — {resp.text[:300]}")
        except Exception as e:
            logger.error(f"send_with_keyboard error: {e}")
    return None


async def edit_message(
    chat_id: str,
    message_id: int,
    text: str,
    keyboard: list[list[dict]] | None = None,
) -> bool:
    """Edit a previously-sent message in place. Used to navigate menus
    without filling the chat with new messages."""
    if not settings.TELEGRAM_BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/editMessageText"
    body = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if keyboard is not None:
        body["reply_markup"] = {"inline_keyboard": keyboard}
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(url, json=body)
            if resp.status_code == 200 and resp.json().get("ok"):
                return True
            # 400 "message is not modified" is common when nothing
            # changed — silent success.
            data = resp.json()
            desc = (data.get("description") or "").lower()
            if "not modified" in desc:
                return True
            logger.warning(f"edit_message: {resp.status_code} — {resp.text[:200]}")
        except Exception as e:
            logger.error(f"edit_message error: {e}")
    return False


async def answer_callback_query(
    callback_id: str,
    text: str | None = None,
    show_alert: bool = False,
) -> bool:
    """Acknowledge a button press. Required by Telegram — the loading
    spinner on the user's button stops only after this. Optional
    text shows as a small toast / popup."""
    if not settings.TELEGRAM_BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    body = {"callback_query_id": callback_id}
    if text:
        body["text"] = text[:200]
        body["show_alert"] = show_alert
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.post(url, json=body)
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"answer_callback_query error: {e}")
    return False


# ─── Formatting helpers ──────────────────────────────────────────────

def _fmt_usd(v: float | None, default: str = "—") -> str:
    if v is None or v == 0:
        return default
    n = float(v)
    if abs(n) >= 1_000_000:
        return f"${n / 1_000_000:.2f}M"
    if abs(n) >= 1_000:
        return f"${n / 1_000:.1f}k"
    return f"${n:,.2f}" if abs(n) < 1 else f"${n:,.0f}"


def _short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if addr and len(addr) > 12 else (addr or "")


def _format_convergence(event: dict) -> str:
    """event: {mint, symbol, name, market:{mc,liq,vol_24h,price},
              wallets:[{address, role, confidence, value_usd}],
              entity_count, cabal_count, trigger_reason}"""
    mint = event["mint"]
    symbol = event.get("symbol") or ""
    name = event.get("name") or ""
    mkt = event.get("market") or {}
    wallets = event.get("wallets") or []
    cabal_count = event.get("cabal_count", len(wallets))

    title_label = symbol or _short(mint)
    title = f"🚨 <b>Cabal convergence</b> · {title_label}"
    if name and name != symbol:
        title += f" <i>({name})</i>"

    wallet_lines = []
    for w in wallets[:8]:
        conf = w.get("confidence", 0.0)
        conf_bit = f" ★{conf:.1f}" if conf else ""
        role_short = (w.get("role") or "unknown").replace("cabal_", "")
        size = w.get("value_usd", 0.0) or 0.0
        size_bit = f" · {_fmt_usd(size)}" if size else ""
        wallet_lines.append(
            f"  • <code>{_short(w['address'])}</code>"
            f" <i>{role_short}</i>{conf_bit}{size_bit}"
        )
    if len(wallets) > 8:
        wallet_lines.append(f"  …and {len(wallets) - 8} more")

    market_lines = []
    if mkt.get("market_cap_usd"):
        market_lines.append(f"<b>MC:</b> {_fmt_usd(mkt['market_cap_usd'])}")
    if mkt.get("liquidity_usd"):
        market_lines.append(f"<b>Liq:</b> {_fmt_usd(mkt['liquidity_usd'])}")
    if mkt.get("volume_24h_usd"):
        market_lines.append(f"<b>Vol 24h:</b> {_fmt_usd(mkt['volume_24h_usd'])}")
    if mkt.get("price_usd"):
        price = mkt["price_usd"]
        price_str = (
            f"${price:.8f}" if price < 0.01
            else f"${price:.6f}" if price < 1
            else f"${price:,.4f}"
        )
        market_lines.append(f"<b>Price:</b> {price_str}")
    market_line = " · ".join(market_lines) if market_lines else "<i>market data unavailable</i>"

    entity_count = event.get("entity_count")
    cabal_line = f"<b>Buyers:</b> {cabal_count} wallets"
    if entity_count and entity_count < cabal_count:
        cabal_line += f" ({entity_count} distinct funder cluster{'s' if entity_count != 1 else ''})"

    reason = event.get("trigger_reason") or ""

    return (
        f"{title}\n\n"
        f"<b>CA:</b> <code>{mint}</code>\n"
        f"{market_line}\n"
        f"{cabal_line}\n"
        + ("\n" + "\n".join(wallet_lines) if wallet_lines else "")
        + (f"\n\n<i>{reason}</i>" if reason else "")
        + f"\n\n"
        f'<a href="https://dexscreener.com/solana/{mint}">DEX Screener</a> · '
        f'<a href="https://gmgn.ai/sol/token/{mint}">GMGN</a> · '
        f'<a href="https://pump.fun/coin/{mint}">pump.fun</a> · '
        f'<a href="https://solscan.io/token/{mint}">Solscan</a>'
    )


# ─── Sniper-solo format ──────────────────────────────────────────────

def _format_sniper_solo(event: dict) -> str:
    """event: {mint, wallet, value_usd, role, confidence,
              avg_buy_size_usd, avg_exit_multiplier, best_mint_profit_usd,
              win_count, loss_count, win_rate, net_profit_usd,
              mints_closed, symbol, name, market}"""
    mint = event["mint"]
    wallet = event["wallet"]
    symbol = event.get("symbol") or ""
    name = event.get("name") or ""
    mkt = event.get("market") or {}

    title_label = symbol or _short(mint)
    title = f"🎯 <b>Sniper solo buy</b> · {title_label}"
    if name and name != symbol:
        title += f" <i>({name})</i>"

    market_lines = []
    if mkt.get("market_cap_usd"):
        market_lines.append(f"<b>MC:</b> {_fmt_usd(mkt['market_cap_usd'])}")
    if mkt.get("liquidity_usd"):
        market_lines.append(f"<b>Liq:</b> {_fmt_usd(mkt['liquidity_usd'])}")
    if mkt.get("volume_24h_usd"):
        market_lines.append(f"<b>Vol 24h:</b> {_fmt_usd(mkt['volume_24h_usd'])}")
    market_line = " · ".join(market_lines) if market_lines else "<i>no pair data yet</i>"

    win_pct = int(event.get("win_rate", 0) * 100)
    win_count = event.get("win_count", 0)
    loss_count = event.get("loss_count", 0)

    return (
        f"{title}\n\n"
        f"<b>CA:</b> <code>{mint}</code>\n"
        f"{market_line}\n"
        f"<b>Wallet:</b> <code>{_short(wallet)}</code>"
        f" ★{event.get('confidence', 0):.1f}"
        f"\n<b>This buy:</b> {_fmt_usd(event.get('value_usd'))}"
        f"\n\n"
        f"<b>Sniper profile:</b>\n"
        f"  • Avg entry: {_fmt_usd(event.get('avg_buy_size_usd'))}\n"
        f"  • Avg exit: {event.get('avg_exit_multiplier', 0):.1f}x\n"
        f"  • Win rate: {win_pct}% ({win_count}W / {loss_count}L over "
        f"{event.get('mints_closed', 0)} closed positions)\n"
        f"  • Best mint: +{_fmt_usd(event.get('best_mint_profit_usd'))}\n"
        f"  • Net lifetime: {_fmt_usd(event.get('net_profit_usd'))}\n\n"
        f'<a href="https://dexscreener.com/solana/{mint}">DEX Screener</a> · '
        f'<a href="https://gmgn.ai/sol/token/{mint}">GMGN</a> · '
        f'<a href="https://pump.fun/coin/{mint}">pump.fun</a> · '
        f'<a href="https://solscan.io/account/{wallet}">Wallet</a>'
    )


async def fire_sniper_solo_alert(event: dict, db: Session | None = None) -> int | None:
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    mint = event["mint"]
    symbol = event.get("symbol") or None
    mkt = event.get("market") or {}
    reason = (
        f"Sniper wallet {_short(event['wallet'])} (avg "
        f"{_fmt_usd(event.get('avg_buy_size_usd'))} entry, "
        f"{event.get('avg_exit_multiplier', 0):.1f}x avg exit) "
        f"bought a new mint."
    )

    try:
        alert = Alert(
            contract_address=mint,
            token_symbol=(symbol[:128] if symbol else None),
            alert_type="sniper_solo",
            trigger_reason=reason,
            mc_at_alert=mkt.get("market_cap_usd") or None,
            price_at_alert=mkt.get("price_usd") or None,
            telegram_sent=False,
            fired_at=datetime.utcnow(),
        )
        db.add(alert)
        db.commit()
    except Exception as e:
        logger.error(f"fire_sniper_solo_alert: Alert INSERT failed: {e}")
        try:
            db.rollback()
        finally:
            if close_db:
                db.close()
        return None

    try:
        message = _format_sniper_solo(event)
        msg_id = await send_telegram_message(message)
        if msg_id is not None:
            alert.telegram_sent = True
            alert.telegram_message_id = msg_id
            db.commit()
    except Exception as e:
        logger.error(f"fire_sniper_solo_alert: Telegram send failed: {e}")
    finally:
        if close_db:
            db.close()
    return alert.id


# ─── Cabal exit alert ────────────────────────────────────────────────

def _format_cabal_exit(event: dict) -> str:
    mint = event["mint"]
    symbol = event.get("symbol") or ""
    mkt = event.get("market") or {}
    title_label = symbol or _short(mint)
    title = f"⚠️ <b>Cabal exiting</b> · {title_label}"

    market_line = ""
    if mkt.get("market_cap_usd"):
        market_line = f"<b>MC now:</b> {_fmt_usd(mkt['market_cap_usd'])}"
        if mkt.get("liquidity_usd"):
            market_line += f" · <b>Liq:</b> {_fmt_usd(mkt['liquidity_usd'])}"

    wallet_lines = []
    for w in event["wallets"][:8]:
        size = w.get("value_usd", 0.0) or 0.0
        size_bit = f" · {_fmt_usd(size)}" if size else ""
        wallet_lines.append(
            f"  • <code>{_short(w['address'])}</code>{size_bit}"
        )

    return (
        f"{title}\n\n"
        f"<b>CA:</b> <code>{mint}</code>\n"
        + (market_line + "\n" if market_line else "")
        + f"<b>Sellers:</b> {event['cabal_count']} wallets from the same cluster "
        + f"sold {_fmt_usd(event.get('total_value_usd', 0))} "
        + f"in the last {event.get('window_min', 10)}m\n"
        + "\n" + "\n".join(wallet_lines)
        + f"\n\n"
        f'<a href="https://dexscreener.com/solana/{mint}">DEX Screener</a> · '
        f'<a href="https://gmgn.ai/sol/token/{mint}">GMGN</a>'
    )


# ─── Solana freshie / dormant SWARM alerts ──────────────────────────

_CHAIN_LABELS = {"solana": "SOL", "eth": "ETH", "bsc": "BSC"}


def _format_swarm(event: dict, kind: str) -> str:
    mint = event["mint"]
    symbol = event.get("symbol") or _short(mint)
    chain = (event.get("chain") or "solana").lower()
    chain_label = _CHAIN_LABELS.get(chain, chain.upper())
    buyers = event.get("buyers") or []
    n = len(buyers)
    mc = event.get("mc_usd") or 0
    liq = event.get("liq_usd") or 0
    price = event.get("price_usd") or 0

    if kind == "freshie":
        title = f"👶 <b>Freshie swarm</b> · {chain_label} · <b>{symbol}</b>"
        descriptor = f"{n} fresh wallets bought"
    else:
        title = f"😴 <b>Dormants waking up</b> · {chain_label} · <b>{symbol}</b>"
        descriptor = f"{n} long-dormant wallets bought"

    market_lines = []
    if mc:
        market_lines.append(f"<b>MC:</b> {_fmt_usd(mc)}")
    if liq:
        market_lines.append(f"<b>Liq:</b> {_fmt_usd(liq)}")
    if price:
        price_str = (
            f"${price:.8f}" if price < 0.01
            else f"${price:.6f}" if price < 1
            else f"${price:,.4f}"
        )
        market_lines.append(f"<b>Px:</b> {price_str}")
    market_line = " · ".join(market_lines) if market_lines else ""

    # Top 6 buyer wallets by buy size
    top = sorted(buyers, key=lambda b: -float(b.get("value_usd") or 0))[:6]
    wallet_lines = []
    for b in top:
        v = float(b.get("value_usd") or 0)
        sz = f" · {_fmt_usd(v)}" if v else ""
        wallet_lines.append(f"  • <code>{_short(b['wallet'])}</code>{sz}")
    if n > len(top):
        wallet_lines.append(f"  …and {n - len(top)} more")

    # Chain-aware link block
    if chain == "solana":
        links = (
            f'<a href="https://dexscreener.com/solana/{mint}">DEX Screener</a> · '
            f'<a href="https://gmgn.ai/sol/token/{mint}">GMGN</a> · '
            f'<a href="https://pump.fun/coin/{mint}">pump.fun</a>'
        )
    elif chain == "eth":
        links = (
            f'<a href="https://dexscreener.com/ethereum/{mint}">DEX Screener</a> · '
            f'<a href="https://etherscan.io/token/{mint}">Etherscan</a> · '
            f'<a href="https://www.dextools.io/app/en/ether/pair-explorer/{mint}">DEXTools</a>'
        )
    elif chain == "bsc":
        links = (
            f'<a href="https://dexscreener.com/bsc/{mint}">DEX Screener</a> · '
            f'<a href="https://bscscan.com/token/{mint}">BscScan</a> · '
            f'<a href="https://www.dextools.io/app/en/bnb/pair-explorer/{mint}">DEXTools</a>'
        )
    else:
        links = f'<a href="https://dexscreener.com/search?q={mint}">DEX Screener</a>'

    return (
        f"{title}\n\n"
        f"<b>CA:</b> <code>{mint}</code>\n"
        + (market_line + "\n" if market_line else "")
        + f"<b>{descriptor}</b> in the last hour\n"
        + ("\n" + "\n".join(wallet_lines) if wallet_lines else "")
        + f"\n\n{links}"
    )


async def _fire_swarm(event: dict, kind: str, db: Session | None = None,
                      alert_type: str | None = None) -> int | None:
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    mint = event["mint"]
    symbol = event.get("symbol")
    n = len(event.get("buyers") or [])
    if alert_type is None:
        alert_type = "freshie_swarm" if kind == "freshie" else "dormant_swarm"
    label = "fresh" if kind == "freshie" else "long-dormant"

    try:
        alert = Alert(
            contract_address=mint,
            token_symbol=(symbol[:128] if symbol else None),
            alert_type=alert_type,
            trigger_reason=f"{n} {label} wallets bought {symbol or mint[:8]}",
            mc_at_alert=event.get("mc_usd") or None,
            price_at_alert=event.get("price_usd") or None,
            telegram_sent=False,
            fired_at=datetime.utcnow(),
        )
        db.add(alert)
        db.commit()
    except Exception as e:
        logger.error(f"_fire_swarm({kind}): Alert INSERT failed: {e}")
        try:
            db.rollback()
        finally:
            if close_db:
                db.close()
        return None

    try:
        msg_id = await send_telegram_message(_format_swarm(event, kind))
        if msg_id is not None:
            alert.telegram_sent = True
            alert.telegram_message_id = msg_id
            db.commit()
    except Exception as e:
        logger.error(f"_fire_swarm({kind}): Telegram send failed: {e}")
    finally:
        if close_db:
            db.close()
    return alert.id


async def fire_freshie_swarm_alert(event: dict, db: Session | None = None,
                                   alert_type: str | None = None) -> int | None:
    return await _fire_swarm(event, "freshie", db, alert_type=alert_type)


async def fire_dormant_swarm_alert(event: dict, db: Session | None = None,
                                   alert_type: str | None = None) -> int | None:
    return await _fire_swarm(event, "dormant", db, alert_type=alert_type)


# ─── Pump.fun migration alert ────────────────────────────────────────

def _format_migration(event: dict) -> str:
    mint = event["mint"]
    symbol = event.get("symbol") or _short(mint)
    name = event.get("name") or ""
    mc = event.get("mc_usd") or 0
    seconds = event.get("seconds_alive") or 0

    title = f"🎓 <b>Pump.fun migration</b> · <b>{symbol}</b>"
    if name and name != symbol:
        title += f" <i>({name})</i>"

    age_str = ""
    if seconds:
        if seconds < 60:
            age_str = f"{int(seconds)}s"
        elif seconds < 3600:
            age_str = f"{int(seconds // 60)}m"
        elif seconds < 86400:
            age_str = f"{seconds / 3600:.1f}h"
        else:
            age_str = f"{seconds / 86400:.1f}d"

    info_lines = []
    if mc:
        info_lines.append(f"<b>MC:</b> {_fmt_usd(mc)}")
    if age_str:
        info_lines.append(f"<b>Age:</b> {age_str}")
    info_line = " · ".join(info_lines) if info_lines else ""

    socials = []
    if event.get("twitter"):
        socials.append(f'<a href="{event["twitter"]}">𝕏</a>')
    if event.get("telegram"):
        socials.append(f'<a href="{event["telegram"]}">TG</a>')
    if event.get("website"):
        socials.append(f'<a href="{event["website"]}">Web</a>')
    socials_line = " · ".join(socials) if socials else ""

    return (
        f"{title}\n\n"
        f"<b>CA:</b> <code>{mint}</code>\n"
        + (info_line + "\n" if info_line else "")
        + (f"<b>Socials:</b> {socials_line}\n" if socials_line else "")
        + "\n"
        f'<a href="https://dexscreener.com/solana/{mint}">DEX Screener</a> · '
        f'<a href="https://gmgn.ai/sol/token/{mint}">GMGN</a> · '
        f'<a href="https://pump.fun/coin/{mint}">pump.fun</a>'
    )


async def fire_migration_alert(event: dict, db: Session | None = None) -> int | None:
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    mint = event["mint"]
    symbol = event.get("symbol")
    try:
        alert = Alert(
            contract_address=mint,
            token_symbol=(symbol[:128] if symbol else None),
            alert_type="pump_migration",
            trigger_reason=f"{symbol or mint[:8]} graduated from pump.fun bonding curve",
            mc_at_alert=event.get("mc_usd") or None,
            telegram_sent=False,
            fired_at=datetime.utcnow(),
        )
        db.add(alert)
        db.commit()
    except Exception as e:
        logger.error(f"fire_migration_alert: Alert INSERT failed: {e}")
        try:
            db.rollback()
        finally:
            if close_db:
                db.close()
        return None

    try:
        msg_id = await send_telegram_message(_format_migration(event))
        if msg_id is not None:
            alert.telegram_sent = True
            alert.telegram_message_id = msg_id
            db.commit()
    except Exception as e:
        logger.error(f"fire_migration_alert: Telegram send failed: {e}")
    finally:
        if close_db:
            db.close()
    return alert.id


# ─── Hyperliquid whale-trade alert ───────────────────────────────────

def _format_hl_whale(event: dict) -> str:
    coin = event["coin"]
    side = (event.get("side") or "").upper()
    side_emoji = "🟢 LONG" if side == "LONG" else "🔴 SHORT" if side == "SHORT" else side
    notional = float(event.get("notional_usd") or 0)
    px = float(event.get("px") or 0)
    sz = float(event.get("sz") or 0)
    actor = event.get("actor") or ""
    fresh = event.get("is_fresh", False)
    fill_count = event.get("fill_count", 0)

    fresh_tag = ""
    if fresh:
        fresh_tag = f"  🆕 FRESH ({fill_count} prior fills)"

    # Coin links — Hyperliquid uses uppercase ticker in its app URL
    hl_token_url = f"https://app.hyperliquid.xyz/trade/{coin}"
    hl_addr_url = f"https://app.hyperliquid.xyz/explorer/address/{actor}"
    arb_url = f"https://arbiscan.io/address/{actor}"

    market_lines = []
    if event.get("mark_px"):
        market_lines.append(f"<b>Mark:</b> ${event['mark_px']:,.6g}")
    if event.get("day_volume"):
        market_lines.append(f"<b>24h vol:</b> {_fmt_usd(event['day_volume'])}")
    if event.get("open_interest"):
        market_lines.append(f"<b>OI:</b> {_fmt_usd(event['open_interest'])}")
    if event.get("funding"):
        funding_pct = event["funding"] * 100
        sign = "+" if funding_pct >= 0 else ""
        market_lines.append(f"<b>Fund:</b> {sign}{funding_pct:.4f}%")
    market_line = " · ".join(market_lines) if market_lines else ""

    px_str = (
        f"${px:.8f}" if px and px < 0.01
        else f"${px:.6f}" if px and px < 1
        else f"${px:,.4f}" if px else "—"
    )

    return (
        f"🐋 <b>Hyperliquid whale</b> · <b>{coin}</b> {side_emoji}\n\n"
        f"<b>Size:</b> {_fmt_usd(notional)} ({sz:,.2f} {coin}) @ {px_str}\n"
        f"<b>Wallet:</b> <code>{_short(actor)}</code>{fresh_tag}\n"
        + (market_line + "\n" if market_line else "")
        + f"\n"
        f'<a href="{hl_token_url}">Hyperliquid</a> · '
        f'<a href="{hl_addr_url}">HL Explorer</a> · '
        f'<a href="{arb_url}">Arbiscan</a>'
    )


async def fire_hl_whale_alert(event: dict, db: Session | None = None) -> int | None:
    """Persist + send a Hyperliquid whale alert. Dedup key (coin|side)
    is stored in `contract_address` so the dispatcher's existing
    24h/per-mint dedup works as a per-(coin,side) dedup for HL."""
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    coin = event["coin"]
    side = event.get("side", "")
    dedup_key = event.get("dedup_key") or f"hl|{coin}|{side}"
    notional = float(event.get("notional_usd") or 0)
    fresh_tag = " (fresh)" if event.get("is_fresh") else ""
    reason = (
        f"{coin} {side.upper()} {_fmt_usd(notional)}{fresh_tag} "
        f"by {_short(event.get('actor') or '')}"
    )
    try:
        alert = Alert(
            contract_address=dedup_key[:64],
            token_symbol=coin[:128],
            alert_type="hl_whale_trade",
            trigger_reason=reason,
            telegram_sent=False,
            fired_at=datetime.utcnow(),
        )
        db.add(alert)
        db.commit()
    except Exception as e:
        logger.error(f"fire_hl_whale_alert: Alert INSERT failed: {e}")
        try:
            db.rollback()
        finally:
            if close_db:
                db.close()
        return None

    try:
        msg_id = await send_telegram_message(_format_hl_whale(event))
        if msg_id is not None:
            alert.telegram_sent = True
            alert.telegram_message_id = msg_id
            db.commit()
    except Exception as e:
        logger.error(f"fire_hl_whale_alert: Telegram send failed: {e}")
    finally:
        if close_db:
            db.close()
    return alert.id


# ─── Cabal exit alert ────────────────────────────────────────────────

async def fire_cabal_exit_alert(event: dict, db: Session | None = None) -> int | None:
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    mint = event["mint"]
    symbol = event.get("symbol") or None
    mkt = event.get("market") or {}
    reason = (
        f"{event['cabal_count']} wallets from the same cluster sold "
        f"{_fmt_usd(event.get('total_value_usd', 0))} in "
        f"{event.get('window_min', 10)}m"
    )
    try:
        alert = Alert(
            contract_address=mint,
            token_symbol=(symbol[:128] if symbol else None),
            alert_type="cabal_exit",
            trigger_reason=reason,
            mc_at_alert=mkt.get("market_cap_usd") or None,
            price_at_alert=mkt.get("price_usd") or None,
            telegram_sent=False,
            fired_at=datetime.utcnow(),
        )
        db.add(alert)
        db.commit()
    except Exception as e:
        logger.error(f"fire_cabal_exit_alert: Alert INSERT failed: {e}")
        try:
            db.rollback()
        finally:
            if close_db:
                db.close()
        return None

    try:
        msg_id = await send_telegram_message(_format_cabal_exit(event))
        if msg_id is not None:
            alert.telegram_sent = True
            alert.telegram_message_id = msg_id
            db.commit()
    except Exception as e:
        logger.error(f"fire_cabal_exit_alert: Telegram send failed: {e}")
    finally:
        if close_db:
            db.close()
    return alert.id


# ─── Entry point used by the alert dispatcher ────────────────────────

async def fire_cabal_alert(event: dict, db: Session | None = None) -> int | None:
    """Persist + send ONE convergence alert. Returns the Alert id on
    success (row persisted; Telegram delivery is best-effort), None
    on DB write failure.

    Persisting the Alert row is the FIRST side effect — dedup relies
    on it. If Telegram blips, we still have the row so we don't
    double-fire next cycle.
    """
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    mint = event["mint"]
    symbol = event.get("symbol") or None
    mkt = event.get("market") or {}
    reason = event.get("trigger_reason") or (
        f"{event.get('cabal_count', 0)} same-cabal wallets bought "
        f"(MC {_fmt_usd(mkt.get('market_cap_usd'))})"
    )

    try:
        alert = Alert(
            contract_address=mint,
            token_symbol=(symbol[:128] if symbol else None),
            alert_type="cabal_convergence",
            trigger_reason=reason,
            mc_at_alert=mkt.get("market_cap_usd") or None,
            price_at_alert=mkt.get("price_usd") or None,
            telegram_sent=False,
            fired_at=datetime.utcnow(),
        )
        db.add(alert)
        db.commit()
    except Exception as e:
        logger.error(
            f"fire_cabal_alert: Alert INSERT failed for mint {mint[:10]}: {e}"
        )
        try:
            db.rollback()
        finally:
            if close_db:
                db.close()
        return None

    try:
        message = _format_convergence(event)
        msg_id = await send_telegram_message(message)
        if msg_id is not None:
            alert.telegram_sent = True
            alert.telegram_message_id = msg_id
            db.commit()
    except Exception as e:
        logger.error(f"fire_cabal_alert: Telegram send failed: {e}")
    finally:
        if close_db:
            db.close()
    return alert.id


# ─── Jupiter DCA order alert ────────────────────────────────────────

def _format_cycle_freq(seconds: int) -> str:
    if seconds <= 0:
        return "unknown"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        h = seconds / 3600
        return f"{h:.1f}h" if h != int(h) else f"{int(h)}h"
    d = seconds / 86400
    return f"{d:.1f}d" if d != int(d) else f"{int(d)}d"


def _format_dca_order(event: dict) -> str:
    mint = event["mint"]
    symbol = event.get("symbol") or ""
    name = event.get("name") or ""
    mkt = event.get("market") or {}
    wallet = event.get("user_wallet") or ""
    input_token = event.get("input_token") or "SOL"
    value_usd = event.get("dca_value_usd") or 0
    dca_amount = event.get("dca_amount") or 0
    per_cycle = event.get("per_cycle") or 0
    num_cycles = event.get("num_cycles") or 0
    cycle_freq = event.get("cycle_freq_sec") or 0

    title_label = symbol or _short(mint)
    title = f"💰 <b>Large DCA order</b> · {title_label}"
    if name and name != symbol:
        title += f" <i>({name})</i>"

    market_lines = []
    if mkt.get("market_cap_usd"):
        market_lines.append(f"<b>MC:</b> {_fmt_usd(mkt['market_cap_usd'])}")
    if mkt.get("liquidity_usd"):
        market_lines.append(f"<b>Liq:</b> {_fmt_usd(mkt['liquidity_usd'])}")
    if mkt.get("price_usd"):
        price = mkt["price_usd"]
        price_str = (
            f"${price:.8f}" if price < 0.01
            else f"${price:.6f}" if price < 1
            else f"${price:,.4f}"
        )
        market_lines.append(f"<b>Px:</b> {price_str}")
    market_line = " · ".join(market_lines) if market_lines else "<i>no pair data yet</i>"

    dca_lines = [f"<b>DCA size:</b> {_fmt_usd(value_usd)} ({dca_amount:,.2f} {input_token})"]
    if per_cycle and num_cycles and cycle_freq:
        freq_str = _format_cycle_freq(cycle_freq)
        dca_lines.append(
            f"<b>Schedule:</b> {per_cycle:,.2f} {input_token} × "
            f"{num_cycles} cycles, every {freq_str}"
        )
    dca_lines.append(f"<b>Flow:</b> {input_token} → {title_label}")

    wallet_lines = [f"<b>Wallet:</b> <code>{_short(wallet)}</code>"]
    is_fresh = event.get("is_fresh")
    is_dormant = event.get("is_dormant")
    tx_count = event.get("tx_count", 0)
    if is_fresh:
        wallet_lines.append(f"  🆕 Fresh wallet ({tx_count} txs)")
    elif is_dormant:
        wallet_lines.append(f"  😴 Dormant wallet woke up ({tx_count} txs)")
    else:
        wallet_lines.append(f"  ({tx_count} txs)")

    if event.get("cex_funded"):
        cex_label = event.get("cex_label") or "CEX"
        cex_sol = event.get("cex_amount_sol") or 0
        wallet_lines.append(
            f"  💎 <b>CEX-funded:</b> {cex_label}"
            + (f" — {cex_sol:,.1f} SOL" if cex_sol else "")
        )

    return (
        f"{title}\n\n"
        f"<b>CA:</b> <code>{mint}</code>\n"
        f"{market_line}\n\n"
        + "\n".join(dca_lines)
        + "\n\n"
        + "\n".join(wallet_lines)
        + f"\n\n"
        f'<a href="https://dexscreener.com/solana/{mint}">DEX Screener</a> · '
        f'<a href="https://gmgn.ai/sol/token/{mint}">GMGN</a> · '
        f'<a href="https://solscan.io/account/{wallet}">Wallet</a> · '
        f'<a href="https://solscan.io/tx/{event.get("signature", "")}">Tx</a>'
    )


async def fire_dca_order_alert(
    event: dict, db: Session | None = None,
) -> int | None:
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    mint = event["mint"]
    symbol = event.get("symbol")
    mkt = event.get("market") or {}
    value_usd = event.get("dca_value_usd") or 0
    cex_tag = f" (CEX-funded: {event['cex_label']})" if event.get("cex_funded") else ""
    fresh_tag = " (fresh wallet)" if event.get("is_fresh") else ""
    reason = (
        f"Jupiter DCA {_fmt_usd(value_usd)} on "
        f"{symbol or mint[:8]}{fresh_tag}{cex_tag}"
    )

    try:
        alert = Alert(
            contract_address=mint,
            token_symbol=(symbol[:128] if symbol else None),
            alert_type="dca_order",
            trigger_reason=reason,
            mc_at_alert=mkt.get("market_cap_usd") or None,
            price_at_alert=mkt.get("price_usd") or None,
            telegram_sent=False,
            fired_at=datetime.utcnow(),
        )
        db.add(alert)
        db.commit()
    except Exception as e:
        logger.error(f"fire_dca_order_alert: Alert INSERT failed: {e}")
        try:
            db.rollback()
        finally:
            if close_db:
                db.close()
        return None

    try:
        msg_id = await send_telegram_message(_format_dca_order(event))
        if msg_id is not None:
            alert.telegram_sent = True
            alert.telegram_message_id = msg_id
            db.commit()
    except Exception as e:
        logger.error(f"fire_dca_order_alert: Telegram send failed: {e}")
    finally:
        if close_db:
            db.close()
    return alert.id
