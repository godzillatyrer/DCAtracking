"""Alert pipeline: Telegram (primary), optional Twilio SMS and macOS popups.

All channels are fired simultaneously on their own threads so one slow
channel never delays another (the CLOCKIN alert is latency-critical).

Also home to the ErrorReporter: any component failure is pushed to Telegram
(rate-limited per error key) so you get an error message when the watcher
is *not* working, not just when it finds something.
"""
import html
import json
import logging
import re
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from . import config

log = logging.getLogger("alerts")

# Alert levels: "info" sends Telegram silently, "alert"/"loud" notify.
LEVELS = ("info", "alert", "loud")


class AlertPipeline:
    def __init__(self) -> None:
        self.session = requests.Session()
        self._telegram_failures = 0
        self._telegram_down_alerted = False

    # -- public API ----------------------------------------------------------
    def send(self, text: str, level: str = "alert",
             code_lines: Optional[List[str]] = None,
             category: str = "recon") -> None:
        """Fan out to every configured channel on parallel threads.

        code_lines: lines to render as tap-to-copy <code> blocks on Telegram
        (used for the bare CA line of the CLOCKIN alert).

        category: "launch" for an actual token launch, "health" for watcher
        status, "recon" for supporting on-chain/frontend signals. In the
        default ALERTS_MODE="launches" only launch and health messages are
        delivered; recon is logged and dropped. Defaults to "recon" so a
        newly added alert stays quiet unless deliberately promoted.
        """
        if config.ALERTS_MODE != "all" and category == "recon":
            log.info("SUPPRESSED [recon] %s", text.splitlines()[0])
            return
        log.info("ALERT [%s/%s]\n%s", level, category, text)
        threads = []
        if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
            threads.append(threading.Thread(
                target=self._send_telegram, args=(text, level, code_lines), daemon=True))
        if level == "loud" and config.TWILIO_ACCOUNT_SID and config.TWILIO_TO:
            threads.append(threading.Thread(
                target=self._send_twilio, args=(text,), daemon=True))
        if config.MACOS_ALERTS and sys.platform == "darwin":
            threads.append(threading.Thread(
                target=self._send_macos, args=(text, level), daemon=True))
        for t in threads:
            t.start()
        # Join briefly so short-lived CLI invocations (--test-alert) still
        # deliver; the long-running watcher is unaffected by slow channels.
        for t in threads:
            t.join(timeout=15)

    def send_test(self) -> Dict[str, str]:
        """Deliver a test alert to each channel, returning per-channel results."""
        results: Dict[str, str] = {}
        stamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        text = f"TEST ALERT — stonk watcher alert pipeline OK ({stamp})"
        if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
            err = self._send_telegram(text, "alert", None)
            results["telegram"] = err or "ok"
        else:
            results["telegram"] = "not configured (set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)"
        if config.TWILIO_ACCOUNT_SID and config.TWILIO_TO:
            err = self._send_twilio(text)
            results["twilio"] = err or "ok"
        else:
            results["twilio"] = "not configured (optional)"
        if sys.platform == "darwin":
            if config.MACOS_ALERTS:
                err = self._send_macos(text, "alert")
                results["macos"] = err or "ok"
            else:
                results["macos"] = "disabled (set MACOS_ALERTS=1)"
        else:
            results["macos"] = "not on macOS"
        return results

    # -- channels ------------------------------------------------------------
    def _send_telegram(self, text: str, level: str,
                       code_lines: Optional[List[str]]) -> Optional[str]:
        """Returns None on success, an error string on failure."""
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        body = html.escape(text)
        if code_lines:
            for line in code_lines:
                escaped = html.escape(line)
                body = body.replace(escaped, f"<code>{escaped}</code>", 1)
        payloads = [
            {"chat_id": config.TELEGRAM_CHAT_ID, "text": body, "parse_mode": "HTML",
             "disable_web_page_preview": True,
             "disable_notification": level == "info"},
            # Fallback: plain text in case HTML entities upset the parser.
            {"chat_id": config.TELEGRAM_CHAT_ID, "text": text,
             "disable_web_page_preview": True,
             "disable_notification": level == "info"},
        ]
        last_err = "unknown"
        for payload in payloads:
            for attempt in range(3):
                try:
                    resp = self.session.post(url, json=payload, timeout=10)
                    if resp.status_code == 200:
                        self._telegram_failures = 0
                        if self._telegram_down_alerted:
                            self._telegram_down_alerted = False
                        return None
                    last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    if resp.status_code == 429:
                        retry_after = 2
                        try:
                            retry_after = int(resp.json()["parameters"]["retry_after"])
                        except Exception:
                            pass
                        time.sleep(min(retry_after, 10))
                        continue
                    if 400 <= resp.status_code < 500:
                        break  # bad payload — try the next (plain) payload
                except requests.RequestException as exc:
                    last_err = str(exc)
                    time.sleep(1 + attempt)
        self._telegram_failures += 1
        log.error("telegram send failed: %s", last_err)
        return last_err

    def _send_twilio(self, text: str) -> Optional[str]:
        url = (f"https://api.twilio.com/2010-04-01/Accounts/"
               f"{config.TWILIO_ACCOUNT_SID}/Messages.json")
        try:
            resp = self.session.post(
                url,
                data={"From": config.TWILIO_FROM, "To": config.TWILIO_TO,
                      "Body": text[:1500]},
                auth=(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN),
                timeout=15,
            )
            if resp.status_code in (200, 201):
                return None
            err = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except requests.RequestException as exc:
            err = str(exc)
        log.error("twilio send failed: %s", err)
        return err

    def _send_macos(self, text: str, level: str) -> Optional[str]:
        first_line = text.strip().splitlines()[0][:120]
        script = (f'display notification "{first_line}" '
                  f'with title "STONK WATCHER" sound name "Glass"')
        try:
            subprocess.run(["osascript", "-e", script], timeout=10, check=False)
            if level == "loud":
                subprocess.run(["say", "-v", "Samantha", "clock in detected"],
                               timeout=10, check=False)
            return None
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("macos alert failed: %s", exc)
            return str(exc)


def format_clockin_alert(ca: str, source: str, name: str = "?", symbol: str = "?",
                         supply: str = "?", tx_link: Optional[str] = None,
                         raw: Optional[Any] = None) -> str:
    """CLOCKIN alert optimized for copy-paste speed: bare CA is line one."""
    verdict, note = verdict_for_target(supply)
    banner = {"confirmed": "CONFIRMED", "mismatch": "CANDIDATE",
              "unknown": "CANDIDATE"}[verdict]
    lines = [
        ca,
        f"*** {config.TARGET_TOKEN_NAME} *** {banner} — detected via {source}",
        ca,
        f"{name} ({symbol}) | supply: {supply}",
        f"check: {note}",
        f"token: {config.BLOCKSCOUT_TOKEN_URL.format(ca=ca)}",
        f"site:  {config.LAUNCHER_PAGE_URL}",
    ]
    if tx_link:
        lines.append(f"tx:    {tx_link}")
    if raw is not None:
        try:
            raw_json = json.dumps(raw, separators=(",", ":"))[:1500]
        except (TypeError, ValueError):
            raw_json = str(raw)[:1500]
        lines.append(f"raw:   {raw_json}")
    return "\n".join(lines)


def is_target_token(name: Optional[str], symbol: Optional[str]) -> bool:
    target = config.TARGET_TOKEN_NAME.lower()
    for value in (name, symbol):
        if value and str(value).strip().lstrip("$").lower() == target:
            return True
    return False


def is_test_token(name: Optional[str], symbol: Optional[str]) -> bool:
    """Deliberate test deploys — the team has shipped several."""
    pattern = re.compile(config.TEST_TOKEN_PATTERN, re.I)
    return any(pattern.search(str(v)) for v in (name, symbol) if v)


def verdict_for_target(supply: Optional[str]) -> Tuple[str, str]:
    """Grade a name-matching token against the expected spec.

    A symbol match is a weak signal here: six CLOCKIN variants already exist
    on the StonkBrokers factory (supplies from 1e6 to 1e9) and another squats
    on pools.fun. So the name never confirms anything on its own — it only
    escalates. Set EXPECTED_SUPPLY to make this decisive.

    Returns (verdict, note); verdict is "confirmed", "mismatch" or "unknown".
    A mismatch is never silently upgraded to confirmed.
    """
    expected = config.EXPECTED_SUPPLY
    if not expected:
        return ("unknown",
                "supply not verified (set EXPECTED_SUPPLY to confirm — "
                "several CLOCKIN tickers exist on this chain)")
    actual = str(supply or "").strip()
    if not actual or actual == "?":
        return "unknown", f"supply unavailable; expected {expected}"
    if _same_number(actual, expected):
        return "confirmed", f"supply matches expected {expected}"
    return "mismatch", f"SPEC MISMATCH — supply {actual}, expected {expected}"


def _same_number(a: str, b: str) -> bool:
    try:
        return int(a) == int(b)
    except (TypeError, ValueError):
        return a.strip() == b.strip()


class ErrorReporter:
    """Pushes watcher malfunctions to Telegram, rate-limited per error key."""

    def __init__(self, pipeline: AlertPipeline,
                 cooldown: Optional[int] = None) -> None:
        self.pipeline = pipeline
        self.cooldown = cooldown if cooldown is not None else config.ERROR_ALERT_COOLDOWN
        self._last_sent: Dict[str, float] = {}
        self._lock = threading.Lock()

    def report(self, component: str, message: str, key: Optional[str] = None,
               level: str = "alert") -> None:
        key = key or f"{component}:{message[:80]}"
        now = time.monotonic()
        with self._lock:
            last = self._last_sent.get(key)
            if last is not None and now - last < self.cooldown:
                log.warning("suppressed repeat error [%s]: %s", component, message)
                return
            self._last_sent[key] = now
        self.pipeline.send(f"WATCHER ERROR [{component}]\n{message}", level=level,
                           category="health")

    def report_exception(self, component: str, exc: BaseException) -> None:
        self.report(component, f"{type(exc).__name__}: {exc}",
                    key=f"{component}:{type(exc).__name__}")

    def recovered(self, component: str, message: str) -> None:
        self.pipeline.send(f"WATCHER RECOVERED [{component}]\n{message}",
                           level="info", category="health")
