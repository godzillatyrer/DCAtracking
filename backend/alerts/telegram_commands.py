"""Telegram bot command interface.

Polls Telegram's /getUpdates every few seconds, routes commands
from the authorized chat to handlers, replies inline.

Authorization: only messages from the configured TELEGRAM_CHAT_ID
get a response. Anyone else who DMs the bot is silently ignored.

Cold-start: on first run we discard the backlog (offset=-1 trick)
so a redeploy doesn't replay stale commands.

Commands implemented:
  /help                  — list available commands
  /status                — system + alert health
  /stats                 — alert counts (24h / 7d)
  /settings              — list categories
  /list <category>       — show settings in a category
  /get <KEY>             — current value + description
  /set <KEY> <value>     — set new value (type-coerced)
  /reset <KEY>           — reset to default
  /enable <KEY>          — set bool true
  /disable <KEY>         — set bool false
  /toggle <KEY>          — flip bool
  /pause                 — pause all watcher alerts (master switch)
  /resume                — resume
  /cleanup <event_type>  — wipe ChainAnomaly + Alert rows of a type
"""

import json
import logging
from datetime import datetime, timedelta

import httpx
from sqlalchemy import desc, func

from backend import settings_cache
from backend.alerts.telegram_bot import send_command_reply
from backend.config import settings
from backend.database import SessionLocal
from backend.models.alert import Alert
from backend.models.app_setting import AppSetting
from backend.models.chain_anomaly import ChainAnomaly
from backend.models.scan_log import ScanLog
from backend.models.solana_known_wallet import SolanaKnownWallet
from backend.models.solana_wallet_stats import SolanaWalletStats

logger = logging.getLogger(__name__)


# ─── Update offset persistence ──────────────────────────────────────

_OFFSET_KEY = "_LAST_TG_UPDATE_ID"


def _load_offset() -> int:
    return int(settings_cache.get(_OFFSET_KEY, 0) or 0)


def _save_offset(update_id: int):
    try:
        settings_cache.set_value(_OFFSET_KEY, update_id)
    except Exception as e:
        logger.warning(f"failed to persist TG offset: {e}")


# ─── Telegram /getUpdates polling ────────────────────────────────────

async def _get_updates(offset: int | None) -> list[dict]:
    if not settings.TELEGRAM_BOT_TOKEN:
        return []
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 0, "limit": 50}
    if offset is not None:
        params["offset"] = offset
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    return data.get("result") or []
                logger.warning(f"getUpdates !ok: {data.get('description')}")
            else:
                logger.warning(f"getUpdates HTTP {resp.status_code}")
    except Exception as e:
        logger.error(f"getUpdates error: {e}")
    return []


# ─── Helpers ────────────────────────────────────────────────────────

def _fmt_usd(v) -> str:
    try:
        n = float(v or 0)
    except Exception:
        return str(v)
    if abs(n) >= 1_000_000:
        return f"${n / 1_000_000:.2f}M"
    if abs(n) >= 1_000:
        return f"${n / 1_000:.1f}k"
    return f"${n:,.2f}" if abs(n) < 1 else f"${n:,.0f}"


def _all_settings() -> list[dict]:
    """Returns all settings, excluding internal (category prefix _)."""
    return [s for s in settings_cache.list_all()
            if not (s.get("category") or "").startswith("_")]


def _find_setting(key: str) -> dict | None:
    target = key.upper().strip()
    for s in _all_settings():
        if (s.get("key") or "").upper() == target:
            return s
    return None


# ─── Command handlers ───────────────────────────────────────────────

async def _cmd_help(args: str) -> str:
    return (
        "<b>Solana Cabal Tracker — Bot Commands</b>\n\n"
        "<b>Status</b>\n"
        "  /status — jobs + DB + alert counts\n"
        "  /stats  — alert counts (24h, 7d)\n\n"
        "<b>Settings</b>\n"
        "  /settings — list categories\n"
        "  /list &lt;category&gt; — settings in that category\n"
        "  /get &lt;KEY&gt; — show one setting\n"
        "  /set &lt;KEY&gt; &lt;value&gt; — update a setting\n"
        "  /reset &lt;KEY&gt; — reset to default\n"
        "  /enable / /disable / /toggle &lt;KEY&gt; — bools\n\n"
        "<b>Alert control</b>\n"
        "  /pause — mute all watcher alerts\n"
        "  /resume — re-enable\n"
        "  /cleanup &lt;event_type&gt; — wipe a noisy type\n"
        "    e.g. <code>/cleanup pump_migration</code>\n"
    )


async def _cmd_status(args: str) -> str:
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        # Job ages
        job_names = [
            "solana_graph_walk",
            "wallet_activity_tracker",
            "wallet_stats_aggregator",
            "alert_outcome_tracker",
            "hyperliquid_watcher",
            "freshie_dormant_watcher",
            "eth_freshie_dormant_watcher",
            "bsc_freshie_dormant_watcher",
            "migration_watcher",
        ]
        lines = []
        for j in job_names:
            last = (
                db.query(ScanLog)
                .filter(ScanLog.job_name == j)
                .order_by(desc(ScanLog.started_at))
                .first()
            )
            if last is None:
                lines.append(f"  · <i>{j}</i>: <code>never</code>")
            else:
                age = int((now - last.started_at).total_seconds() // 60) if last.started_at else None
                emoji = "✅" if last.status == "success" else "❌"
                lines.append(f"  {emoji} <i>{j}</i>: {age}m ago")

        # Counts
        tracked = db.query(func.count(SolanaKnownWallet.wallet_address)).scalar() or 0
        snipers = (
            db.query(func.count(SolanaWalletStats.wallet_address))
            .filter(SolanaWalletStats.is_sniper.is_(True))
            .scalar() or 0
        )
        alerts_24h = db.query(func.count(Alert.id)).filter(
            Alert.fired_at >= now - timedelta(hours=24)
        ).scalar() or 0
        alerts_sent = db.query(func.count(Alert.id)).filter(
            Alert.fired_at >= now - timedelta(hours=24),
            Alert.telegram_sent.is_(True),
        ).scalar() or 0
        paused = settings_cache.get("ALERTS_PAUSED", False)

        return (
            f"<b>System status</b>\n"
            f"Alerts: <b>{'🔇 PAUSED' if paused else '🔔 ACTIVE'}</b>\n\n"
            f"<b>Jobs</b>\n" + "\n".join(lines) + "\n\n"
            f"<b>Counts</b>\n"
            f"  · Tracked wallets: {tracked}\n"
            f"  · Snipers: {snipers}\n"
            f"  · Alerts 24h: {alerts_sent} sent / {alerts_24h} fired\n"
        )
    finally:
        db.close()


async def _cmd_stats(args: str) -> str:
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        windows = [("24h", timedelta(hours=24)), ("7d", timedelta(days=7))]
        lines = ["<b>Alert counts by type</b>"]
        for label, td_ in windows:
            cutoff = now - td_
            rows = (
                db.query(Alert.alert_type, func.count(Alert.id))
                .filter(Alert.fired_at >= cutoff)
                .group_by(Alert.alert_type)
                .all()
            )
            lines.append(f"\n<b>{label}</b>:")
            if not rows:
                lines.append("  (none)")
            for at, c in sorted(rows, key=lambda r: -r[1]):
                lines.append(f"  · {at or 'unknown'}: {c}")
        return "\n".join(lines)
    finally:
        db.close()


async def _cmd_settings(args: str) -> str:
    items = _all_settings()
    by_cat: dict[str, int] = {}
    for s in items:
        c = s.get("category") or "uncategorized"
        by_cat[c] = by_cat.get(c, 0) + 1
    lines = ["<b>Settings categories</b>"]
    for cat, n in sorted(by_cat.items()):
        lines.append(f"  · <code>{cat}</code> ({n})")
    lines.append("\nUse <code>/list &lt;category&gt;</code> to see settings.")
    return "\n".join(lines)


async def _cmd_list(args: str) -> str:
    cat = (args or "").strip().lower()
    if not cat:
        return "Usage: <code>/list &lt;category&gt;</code>"
    items = [s for s in _all_settings() if (s.get("category") or "").lower() == cat]
    if not items:
        return f"No settings in category <code>{cat}</code>. Try /settings to see categories."
    lines = [f"<b>{cat.upper()}</b>"]
    for s in items:
        v = s.get("value")
        v_str = "true" if v is True else "false" if v is False else f"{v}"
        lines.append(f"  · <code>{s['key']}</code> = <b>{v_str}</b>")
    lines.append("\nUse <code>/get &lt;KEY&gt;</code> for full details.")
    return "\n".join(lines)


async def _cmd_get(args: str) -> str:
    key = (args or "").strip()
    if not key:
        return "Usage: <code>/get &lt;KEY&gt;</code>"
    s = _find_setting(key)
    if s is None:
        return f"Unknown setting: <code>{key}</code>"
    v = s.get("value")
    d = s.get("default")
    return (
        f"<code>{s['key']}</code> = <b>{v}</b>\n"
        f"<i>Default: {d} · type: {s.get('type')} · category: {s.get('category')}</i>\n\n"
        f"{s.get('description') or ''}"
    )


async def _cmd_set(args: str) -> str:
    parts = (args or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        return "Usage: <code>/set &lt;KEY&gt; &lt;value&gt;</code>"
    key, raw_value = parts[0], parts[1]
    s = _find_setting(key)
    if s is None:
        return f"Unknown setting: <code>{key}</code>"
    old = s.get("value")
    try:
        result = settings_cache.set_value(s["key"], raw_value)
    except Exception as e:
        return f"❌ {e}"
    return f"✅ <code>{result['key']}</code>: <b>{old}</b> → <b>{result['value']}</b>"


async def _cmd_reset(args: str) -> str:
    key = (args or "").strip()
    s = _find_setting(key)
    if s is None:
        return f"Unknown setting: <code>{key}</code>"
    out = settings_cache.reset_default(s["key"])
    if not out:
        return f"❌ no default for <code>{s['key']}</code>"
    return f"✅ <code>{out['key']}</code> reset to <b>{out['value']}</b>"


async def _set_bool(key: str, target: bool) -> str:
    s = _find_setting(key)
    if s is None:
        return f"Unknown setting: <code>{key}</code>"
    if s.get("type") != "bool":
        return f"<code>{s['key']}</code> is {s.get('type')}, not bool"
    out = settings_cache.set_value(s["key"], target)
    return f"✅ <code>{out['key']}</code> = <b>{out['value']}</b>"


async def _cmd_enable(args: str) -> str:
    return await _set_bool((args or "").strip(), True)


async def _cmd_disable(args: str) -> str:
    return await _set_bool((args or "").strip(), False)


async def _cmd_toggle(args: str) -> str:
    s = _find_setting((args or "").strip())
    if s is None:
        return f"Unknown setting: <code>{args}</code>"
    if s.get("type") != "bool":
        return f"<code>{s['key']}</code> is {s.get('type')}, not bool"
    return await _set_bool(s["key"], not bool(s.get("value")))


async def _cmd_pause(args: str) -> str:
    settings_cache.set_value("ALERTS_PAUSED", True)
    return "🔇 <b>Alerts paused.</b> Send /resume to re-enable."


async def _cmd_resume(args: str) -> str:
    settings_cache.set_value("ALERTS_PAUSED", False)
    return "🔔 <b>Alerts resumed.</b>"


_ALLOWED_CLEANUP_TYPES = {
    "pump_migration",
    "freshie_swarm", "dormant_swarm",
    "eth_freshie_swarm", "eth_dormant_swarm",
    "bsc_freshie_swarm", "bsc_dormant_swarm",
    "hl_whale_trade",
    "cabal_convergence", "cabal_exit", "sniper_solo",
}


async def _cmd_cleanup(args: str) -> str:
    target = (args or "").strip()
    if target not in _ALLOWED_CLEANUP_TYPES:
        return (
            "Usage: <code>/cleanup &lt;event_type&gt;</code>\n"
            "Allowed: " + ", ".join(sorted(_ALLOWED_CLEANUP_TYPES))
        )
    db = SessionLocal()
    try:
        deleted_anoms = (
            db.query(ChainAnomaly)
            .filter(ChainAnomaly.event_type == target)
            .delete(synchronize_session=False)
        )
        deleted_alerts = (
            db.query(Alert)
            .filter(Alert.alert_type == target)
            .delete(synchronize_session=False)
        )
        db.commit()
        return (
            f"🧹 Cleaned <b>{target}</b>:\n"
            f"  · Anomalies: {deleted_anoms}\n"
            f"  · Alerts: {deleted_alerts}"
        )
    except Exception as e:
        db.rollback()
        return f"❌ cleanup failed: {e}"
    finally:
        db.close()


_HANDLERS = {
    "help": _cmd_help,
    "start": _cmd_help,
    "status": _cmd_status,
    "stats": _cmd_stats,
    "settings": _cmd_settings,
    "list": _cmd_list,
    "get": _cmd_get,
    "set": _cmd_set,
    "reset": _cmd_reset,
    "enable": _cmd_enable,
    "disable": _cmd_disable,
    "toggle": _cmd_toggle,
    "pause": _cmd_pause,
    "resume": _cmd_resume,
    "cleanup": _cmd_cleanup,
}


# ─── Update loop ─────────────────────────────────────────────────────

async def _process_one_update(u: dict) -> None:
    msg = u.get("message") or u.get("edited_message")
    if not msg:
        return
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")

    # Only respond to the configured chat. Anything else is silently
    # dropped (the bot ignores DMs from random users).
    if not settings.TELEGRAM_CHAT_ID or chat_id != str(settings.TELEGRAM_CHAT_ID):
        logger.info(f"Ignoring TG message from unauthorized chat {chat_id}")
        return

    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return

    # Strip leading slash + any @botname suffix (group-bot style)
    cmd_with_args = text[1:]
    parts = cmd_with_args.split(maxsplit=1)
    cmd = parts[0]
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]
    cmd = cmd.lower()
    args = parts[1] if len(parts) > 1 else ""

    handler = _HANDLERS.get(cmd)
    if handler is None:
        await send_command_reply(
            f"Unknown command: <code>/{cmd}</code>. Try /help.",
            chat_id=chat_id,
        )
        return

    try:
        reply = await handler(args)
    except Exception as e:
        logger.error(f"Command /{cmd} failed: {e}")
        reply = f"❌ Command error: {e}"
    if reply:
        await send_command_reply(reply, chat_id=chat_id)


async def run_telegram_command_poller() -> dict:
    if not settings.TELEGRAM_BOT_TOKEN:
        return {"skipped": "no_token"}

    last = _load_offset()

    # Cold start: discard backlog. We pass offset=-1 to grab only the
    # most recent update, store it, and process nothing on first run.
    if last <= 0:
        primer = await _get_updates(-1)
        if primer:
            _save_offset(primer[-1]["update_id"])
        return {"cold_start": True, "primed_to": (primer[-1]["update_id"] if primer else 0)}

    updates = await _get_updates(last + 1)
    processed = 0
    for u in updates:
        try:
            await _process_one_update(u)
        except Exception as e:
            logger.error(f"command process error: {e}")
        processed += 1
        # Save after each so a crash doesn't replay
        _save_offset(u["update_id"])
    return {"processed": processed}
