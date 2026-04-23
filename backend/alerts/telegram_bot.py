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
            logger.error(f"Telegram API error: {resp.status_code} — {resp.text[:300]}")
        except Exception as e:
            logger.error(f"Telegram error: {e}")
    return None


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


async def fire_sniper_solo_alert(event: dict, db: Session | None = None) -> bool:
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    mint = event["mint"]
    symbol = event.get("symbol") or None
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
        return False

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
    return True


# ─── Entry point used by the alert dispatcher ────────────────────────

async def fire_cabal_alert(event: dict, db: Session | None = None) -> bool:
    """Persist + send ONE convergence alert.

    `event` must contain at minimum: mint, wallets (list of dicts with
    address/role/confidence/value_usd). Market info and symbol/name
    are optional but recommended for formatting.

    Returns True if an alert was persisted (whether or not Telegram
    actually delivered). Returns False only if the DB write failed.

    Persisting the Alert row is the FIRST real side effect — dedup
    relies on it. If Telegram blips, we still want the row recorded
    so we don't double-fire on the next cycle.
    """
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    mint = event["mint"]
    symbol = event.get("symbol") or None
    reason = event.get("trigger_reason") or (
        f"{event.get('cabal_count', 0)} same-cabal wallets bought "
        f"(MC {_fmt_usd(event.get('market', {}).get('market_cap_usd'))})"
    )

    try:
        alert = Alert(
            contract_address=mint,
            token_symbol=(symbol[:128] if symbol else None),
            alert_type="cabal_convergence",
            trigger_reason=reason,
            telegram_sent=False,
            fired_at=datetime.utcnow(),
        )
        db.add(alert)
        db.commit()
    except Exception as e:
        logger.error(
            f"fire_cabal_alert: Alert INSERT failed for mint {mint[:10]}: {e}. "
            "Dedup is broken until this is fixed — aborting send."
        )
        try:
            db.rollback()
        finally:
            if close_db:
                db.close()
        return False

    # Now send — a failure here doesn't break dedup.
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
    return True
