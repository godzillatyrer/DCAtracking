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
from backend.alerts.telegram_bot import (
    answer_callback_query,
    edit_message,
    send_command_reply,
    send_with_keyboard,
)
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
    # `allowed_updates` must be a JSON-encoded array. Without it,
    # Telegram only delivers `message` updates and the menu buttons
    # would be silent.
    params = {
        "timeout": 0, "limit": 50,
        "allowed_updates": json.dumps([
            "message", "edited_message", "callback_query",
        ]),
    }
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
        "💡 Easiest path: send /menu to open the button-driven UI.\n\n"
        "<b>Status</b>\n"
        "  /menu — main button menu\n"
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
        "    e.g. <code>/cleanup cex_accumulation</code>\n"
    )


async def _cmd_status(args: str) -> str:
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        # Job ages
        job_names = [
            "cex_outflow_harvester",
            "accumulation_alerter",
            "solana_graph_walk",
            "wallet_activity_tracker",
            "wallet_stats_aggregator",
            "alert_outcome_tracker",
            "dca_order_watcher",
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
    "cex_accumulation",
    "dca_order",
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


# ─── Inline-keyboard menus ──────────────────────────────────────────
#
# callback_data prefixes (kept short; Telegram caps cb_data at 64 bytes):
#   menu              — main menu
#   status / stats    — info screens
#   settings          — categories list
#   cat:<category>    — settings within a category
#   set:<KEY>         — single-setting view
#   tog:<KEY>         — toggle bool, refresh setting view
#   rst:<KEY>         — reset to default
#   edt:<KEY>         — prompt user via forceReply for new value
#   pause / resume    — master switch
#   cleanup           — cleanup type picker
#   clean:<type>      — confirm picker for a type
#   confclean:<type>  — actually delete
#   noop              — no-op (used for separator labels)


def _kb_main_menu() -> list[list[dict]]:
    paused = bool(settings_cache.get("ALERTS_PAUSED", False))
    pause_btn = (
        {"text": "🔔 Resume alerts", "callback_data": "resume"}
        if paused else
        {"text": "🔇 Pause alerts", "callback_data": "pause"}
    )
    return [
        [{"text": "📊 Status",   "callback_data": "status"},
         {"text": "📈 Stats",    "callback_data": "stats"}],
        [{"text": "⚙️ Settings", "callback_data": "settings"}],
        [pause_btn,
         {"text": "🧹 Cleanup",  "callback_data": "cleanup"}],
        [{"text": "❓ Help",     "callback_data": "help"}],
    ]


def _menu_text() -> str:
    paused = bool(settings_cache.get("ALERTS_PAUSED", False))
    state = "🔇 PAUSED" if paused else "🔔 ACTIVE"
    return (
        f"<b>Solana Cabal Tracker</b>\n"
        f"Alerts: <b>{state}</b>\n\n"
        f"Pick an option below, or send /help for the full text-command list."
    )


def _kb_categories() -> list[list[dict]]:
    items = _all_settings()
    cats: dict[str, int] = {}
    for s in items:
        c = s.get("category") or "uncategorized"
        cats[c] = cats.get(c, 0) + 1
    rows: list[list[dict]] = []
    pairs = sorted(cats.items())
    # 2 buttons per row
    for i in range(0, len(pairs), 2):
        row = []
        for cat, n in pairs[i:i + 2]:
            row.append({"text": f"{cat} ({n})", "callback_data": f"cat:{cat}"})
        rows.append(row)
    rows.append([{"text": "⬅️ Back", "callback_data": "menu"}])
    return rows


def _kb_category(cat: str) -> list[list[dict]]:
    items = [s for s in _all_settings()
             if (s.get("category") or "").lower() == cat.lower()]
    rows: list[list[dict]] = []
    # 1 button per row — keys are long
    for s in items:
        v = s.get("value")
        v_str = "true" if v is True else "false" if v is False else f"{v}"
        # Truncate the displayed key if very long, but preserve full
        # key in callback_data
        label = f"{s['key']}: {v_str}"
        if len(label) > 50:
            label = label[:48] + "…"
        rows.append([{"text": label, "callback_data": f"set:{s['key']}"}])
    rows.append([{"text": "⬅️ Back", "callback_data": "settings"}])
    return rows


def _kb_setting(key: str) -> list[list[dict]]:
    s = _find_setting(key)
    if s is None:
        return [[{"text": "⬅️ Back", "callback_data": "settings"}]]
    rows: list[list[dict]] = []
    if s.get("type") == "bool":
        rows.append([{"text": "🔄 Toggle", "callback_data": f"tog:{s['key']}"}])
    else:
        rows.append([{"text": "✏️ Edit value", "callback_data": f"edt:{s['key']}"}])
    rows.append([{"text": "↺ Reset to default", "callback_data": f"rst:{s['key']}"}])
    cat = s.get("category") or ""
    rows.append([{"text": "⬅️ Back", "callback_data": f"cat:{cat}"}])
    return rows


def _setting_text(key: str) -> str:
    s = _find_setting(key)
    if s is None:
        return f"Unknown setting: <code>{key}</code>"
    v = s.get("value")
    d = s.get("default")
    return (
        f"<b>{s['key']}</b>\n\n"
        f"Current: <code>{v}</code>\n"
        f"Default: <code>{d}</code>\n"
        f"Type: <code>{s.get('type')}</code>\n"
        f"Category: <code>{s.get('category')}</code>\n\n"
        f"<i>{s.get('description') or ''}</i>"
    )


def _kb_cleanup_picker() -> list[list[dict]]:
    rows = []
    types = sorted(_ALLOWED_CLEANUP_TYPES)
    for i in range(0, len(types), 2):
        row = []
        for t in types[i:i + 2]:
            row.append({"text": t, "callback_data": f"clean:{t}"})
        rows.append(row)
    rows.append([{"text": "⬅️ Back", "callback_data": "menu"}])
    return rows


def _kb_cleanup_confirm(t: str) -> list[list[dict]]:
    return [
        [{"text": f"❗ Delete all {t}", "callback_data": f"confclean:{t}"}],
        [{"text": "⬅️ Back", "callback_data": "cleanup"}],
    ]


# ─── /menu and other text-cmd entry points to the menu ─────────────

async def _cmd_menu(args: str) -> str | None:
    """Sends a fresh menu with inline buttons. Returns None because the
    menu reply is sent via send_with_keyboard, not via the standard
    reply path."""
    await send_with_keyboard(_menu_text(), _kb_main_menu())
    return None


_HANDLERS = {
    "help": _cmd_help,
    "start": _cmd_menu,
    "menu": _cmd_menu,
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


# ─── Callback router ────────────────────────────────────────────────

# Edit-value prompts use forceReply. The prompt text encodes the key
# so the reply handler can look it up. Format must match exactly.
_EDIT_PROMPT_PREFIX = "Send new value for "
_EDIT_PROMPT_SUFFIX = " (current: "


def _build_edit_prompt(key: str, current: object) -> str:
    return f"{_EDIT_PROMPT_PREFIX}{key}{_EDIT_PROMPT_SUFFIX}{current})"


def _parse_edit_prompt(text: str) -> str | None:
    if not text or not text.startswith(_EDIT_PROMPT_PREFIX):
        return None
    body = text[len(_EDIT_PROMPT_PREFIX):]
    cut = body.find(_EDIT_PROMPT_SUFFIX)
    if cut < 0:
        return None
    return body[:cut].strip()


async def _handle_callback(cb: dict) -> None:
    cb_id = cb.get("id")
    msg = cb.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    message_id = msg.get("message_id")
    data = (cb.get("data") or "").strip()

    # Auth: only the configured chat
    if not settings.TELEGRAM_CHAT_ID or chat_id != str(settings.TELEGRAM_CHAT_ID):
        await answer_callback_query(cb_id)
        return

    if not data or data == "noop":
        await answer_callback_query(cb_id)
        return

    # Helper closures over the callback
    async def _ack(toast: str | None = None, alert: bool = False):
        await answer_callback_query(cb_id, text=toast, show_alert=alert)

    async def _replace(text: str, kb: list[list[dict]] | None):
        if message_id is None:
            await send_with_keyboard(text, kb or [])
        else:
            await edit_message(chat_id, message_id, text, kb)

    try:
        if data == "menu":
            await _ack()
            await _replace(_menu_text(), _kb_main_menu())
            return

        if data == "help":
            await _ack()
            kb = [[{"text": "⬅️ Back", "callback_data": "menu"}]]
            await _replace(await _cmd_help(""), kb)
            return

        if data == "status":
            await _ack()
            kb = [[{"text": "⬅️ Back", "callback_data": "menu"}]]
            await _replace(await _cmd_status(""), kb)
            return

        if data == "stats":
            await _ack()
            kb = [[{"text": "⬅️ Back", "callback_data": "menu"}]]
            await _replace(await _cmd_stats(""), kb)
            return

        if data == "settings":
            await _ack()
            await _replace("<b>Settings categories</b>", _kb_categories())
            return

        if data.startswith("cat:"):
            cat = data.split(":", 1)[1]
            await _ack()
            text = f"<b>{cat.upper()}</b>\nTap a setting to view / edit."
            await _replace(text, _kb_category(cat))
            return

        if data.startswith("set:"):
            key = data.split(":", 1)[1]
            await _ack()
            await _replace(_setting_text(key), _kb_setting(key))
            return

        if data.startswith("tog:"):
            key = data.split(":", 1)[1]
            s = _find_setting(key)
            if s is None or s.get("type") != "bool":
                await _ack("Not a bool", alert=True)
                return
            new_val = not bool(s.get("value"))
            settings_cache.set_value(key, new_val)
            await _ack(f"Set {key} = {new_val}")
            await _replace(_setting_text(key), _kb_setting(key))
            return

        if data.startswith("rst:"):
            key = data.split(":", 1)[1]
            s = _find_setting(key)
            if s is None:
                await _ack("Unknown key", alert=True)
                return
            settings_cache.reset_default(key)
            await _ack("Reset to default")
            await _replace(_setting_text(key), _kb_setting(key))
            return

        if data.startswith("edt:"):
            key = data.split(":", 1)[1]
            s = _find_setting(key)
            if s is None:
                await _ack("Unknown key", alert=True)
                return
            await _ack()
            # Send a forceReply prompt — Telegram surfaces a reply-to
            # field. The user's reply will arrive as a regular message
            # with reply_to_message set, which we parse to recover key.
            await send_with_keyboard(
                _build_edit_prompt(key, s.get("value")),
                keyboard=[],
                force_reply=True,
            )
            return

        if data == "pause":
            settings_cache.set_value("ALERTS_PAUSED", True)
            await _ack("Paused")
            await _replace(_menu_text(), _kb_main_menu())
            return

        if data == "resume":
            settings_cache.set_value("ALERTS_PAUSED", False)
            await _ack("Resumed")
            await _replace(_menu_text(), _kb_main_menu())
            return

        if data == "cleanup":
            await _ack()
            await _replace(
                "<b>Cleanup</b>\nPick an event type to wipe.",
                _kb_cleanup_picker(),
            )
            return

        if data.startswith("clean:"):
            t = data.split(":", 1)[1]
            if t not in _ALLOWED_CLEANUP_TYPES:
                await _ack("Not allowed", alert=True)
                return
            await _ack()
            await _replace(
                f"<b>Confirm cleanup</b>\n"
                f"Delete ALL <code>{t}</code> alerts + anomaly rows?",
                _kb_cleanup_confirm(t),
            )
            return

        if data.startswith("confclean:"):
            t = data.split(":", 1)[1]
            await _ack("Cleaning…")
            reply = await _cmd_cleanup(t)
            kb = [[{"text": "⬅️ Back", "callback_data": "cleanup"}]]
            await _replace(reply, kb)
            return

        # Unknown
        await _ack(f"Unknown action: {data}", alert=True)
    except Exception as e:
        logger.error(f"callback {data!r} failed: {e}")
        try:
            await _ack(f"Error: {e}", alert=True)
        except Exception:
            pass


# ─── forceReply value-edit handler ───────────────────────────────────

async def _handle_value_reply(msg: dict) -> bool:
    """If `msg` is a reply to one of our edit prompts, apply the new
    value and confirm. Returns True if handled."""
    reply_to = msg.get("reply_to_message")
    if not reply_to:
        return False
    prompt_text = (reply_to.get("text") or "")
    key = _parse_edit_prompt(prompt_text)
    if not key:
        return False
    new_value = (msg.get("text") or "").strip()
    if not new_value:
        await send_command_reply(
            f"Empty value — keeping <code>{key}</code> unchanged."
        )
        return True
    s = _find_setting(key)
    if s is None:
        await send_command_reply(f"Unknown setting: <code>{key}</code>")
        return True
    try:
        result = settings_cache.set_value(s["key"], new_value)
        await send_command_reply(
            f"✅ <code>{result['key']}</code> = <b>{result['value']}</b>"
        )
    except Exception as e:
        await send_command_reply(f"❌ {e}")
    return True


# ─── Update loop ─────────────────────────────────────────────────────

async def _process_one_update(u: dict) -> None:
    # Inline-button presses arrive as callback_query updates.
    cb = u.get("callback_query")
    if cb:
        await _handle_callback(cb)
        return

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

    # forceReply edit-value flow: if this message is a reply to one
    # of our edit prompts, apply the value and we're done.
    if await _handle_value_reply(msg):
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
