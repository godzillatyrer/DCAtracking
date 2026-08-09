"""Track 3 — frontend bundle differ.

Hourly: fetch the launcher page, extract the Vercel deployment id and every
address-shaped string from the HTML plus its /_next/static/chunks/*.js
bundles, diff against the previous snapshot, and Blockscout-check anything
new. This is how we'd learn the factory address hours before T0.
"""
import logging
import re
from datetime import datetime, timezone
from typing import List, Optional, Set

import requests

from . import config
from .alerts import AlertPipeline, ErrorReporter, is_target_token
from .blockscout import BlockscoutClient
from .state import State

log = logging.getLogger("frontend_diff")

DEPLOYMENT_RE = re.compile(r"dpl_[A-Za-z0-9]+")
ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")
CHUNK_RE = re.compile(r"/_next/static/chunks/[^\"'\s\\]+?\.js")

# The launcher factory address is injected at BUILD time from a
# NEXT_PUBLIC_* env var, and is currently an empty string in the shipped
# bundle (launcherFactory:""). Next.js inlines these as string literals, so
# the address appears in the JS the moment the team sets it and redeploys —
# typically before the launchpad opens. Catching that transition hands us
# the factory ahead of any on-chain or API signal.
FACTORY_PATTERNS = [
    re.compile(r"launcherFactory[\"']?\s*:[^,}]{0,160}?(0x[a-fA-F0-9]{40})"),
    re.compile(r"NEXT_PUBLIC_LAUNCHPAD_FACTORY[^,}]{0,160}?(0x[a-fA-F0-9]{40})"),
    re.compile(r"NEXT_PUBLIC_STONK_LAUNCHER_FACTORY_ADDRESS"
               r"[^,}]{0,160}?(0x[a-fA-F0-9]{40})"),
]
FACTORY_START_BLOCK_RE = re.compile(
    r"launcherFactoryStartBlock[\"']?\s*:\s*(?:Number\()?[\"']?(\d{1,12})")

MAX_CHUNKS = 50
MAX_CHUNK_BYTES = 2_000_000
MAX_NEW_CHECKS_PER_CYCLE = 25


class FrontendDiffer:
    name = "frontend_diff"

    def __init__(self, state: State, pipeline: AlertPipeline, errors: ErrorReporter,
                 blockscout: Optional[BlockscoutClient] = None) -> None:
        self.state = state
        self.pipeline = pipeline
        self.errors = errors
        self.blockscout = blockscout or BlockscoutClient()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": config.BROWSER_USER_AGENT})
        self.consecutive_failures = 0
        self.down_alerted = False

    def _fetch_text(self, url: str) -> Optional[str]:
        try:
            resp = self.session.get(url, timeout=20)
            if resp.status_code != 200:
                log.warning("HTTP %d on %s", resp.status_code, url)
                return None
            return resp.text[:MAX_CHUNK_BYTES]
        except requests.RequestException as exc:
            log.warning("fetch failed %s: %s", url, exc)
            return None

    def run_once(self) -> None:
        html_text = self._fetch_text(config.LAUNCHER_PAGE_URL)
        if html_text is None:
            self.consecutive_failures += 1
            if (self.consecutive_failures >= config.FAILURE_ALERT_THRESHOLD
                    and not self.down_alerted):
                self.down_alerted = True
                self.errors.report(
                    self.name,
                    f"Launcher page unreachable ({self.consecutive_failures} "
                    "consecutive failures) — frontend diffing is blind.",
                    key="frontend_down")
            return
        self.consecutive_failures = 0
        if self.down_alerted:
            self.down_alerted = False
            self.errors.recovered(self.name, "launcher page reachable again")

        fstate = self.state.frontend()
        blobs: List[str] = [html_text]
        for chunk_path in list(dict.fromkeys(CHUNK_RE.findall(html_text)))[:MAX_CHUNKS]:
            chunk_text = self._fetch_text(config.API_BASE + chunk_path)
            if chunk_text:
                blobs.append(chunk_text)

        # Deployment id change = the site was redeployed. Worth knowing.
        deployments = DEPLOYMENT_RE.findall(html_text)
        if deployments:
            current = deployments[0]
            previous = fstate.get("deployment_id")
            if current != previous:
                fstate["deployment_id"] = current
                self.state.mark_dirty()
                if previous is not None:
                    self.pipeline.send(
                        f"FRONTEND REDEPLOYED\n{previous} -> {current}\n"
                        f"site: {config.LAUNCHER_PAGE_URL}\n"
                        "Diffing bundles for new contract addresses...",
                        level="alert")

        # Checked before the baseline gate: the factory going from "" to an
        # address is the single highest-value signal on this site, and it
        # must fire even on the very first cycle.
        self._check_launcher_factory(blobs)

        found: Set[str] = set()
        for blob in blobs:
            found.update(a.lower() for a in ADDRESS_RE.findall(blob))

        seen = set(fstate["seen_addresses"])
        negative = set(fstate["negative_cache"])

        # First run: record everything the bundle already contains WITHOUT
        # alerting. The signal here is an address that appears *after* we
        # start watching; on a cold start every pre-existing RWA/stock token,
        # WETH and piece of infra would otherwise be reported as "new" and
        # bury the launch alert. Mirrors the dev-wallet baseline.
        if not fstate.get("baselined"):
            fstate["seen_addresses"] = sorted(seen | found)
            fstate["baselined"] = True
            self.state.mark_dirty()
            self.state.save_if_dirty()
            log.info("baselined %d frontend addresses", len(found))
            self.pipeline.send(
                f"FRONTEND BASELINED\n{len(found)} addresses already in the "
                "launcher bundle recorded as pre-existing (RWA/stock tokens, "
                "WETH, infra). Only addresses that appear from now on will "
                "alert.", level="info")
            return

        new_addresses = sorted(found - seen - negative - config.BORING_ADDRESSES)

        checked = 0
        for addr in new_addresses:
            if checked >= MAX_NEW_CHECKS_PER_CYCLE:
                log.warning("more than %d new addresses this cycle; rest next hour",
                            MAX_NEW_CHECKS_PER_CYCLE)
                break
            checked += 1
            try:
                info = self.blockscout.classify_address(addr)
            except requests.RequestException as exc:
                log.warning("blockscout check failed for %s: %s", addr, exc)
                continue  # retry next cycle — do NOT cache transport failures
            if not info["exists"]:
                # Not deployed on Robinhood Chain: skip silently, cache the
                # negative result to avoid re-checking.
                negative.add(addr)
                fstate["negative_cache"] = sorted(negative)
                self.state.mark_dirty()
                continue
            seen.add(addr)
            fstate["seen_addresses"] = sorted(seen)
            self.state.mark_dirty()
            if info["is_contract"]:
                token = info.get("token") or {}
                name = str(token.get("name") or "?")
                symbol = str(token.get("symbol") or "?")
                if token and not is_target_token(name, symbol):
                    # An existing token added to the site is ordinary listing
                    # churn — Tracks 1 and 2 own token discovery. Keep it on
                    # the record, but silently: the factory we're hunting is
                    # NOT a token, so an untyped contract is the real signal.
                    self.pipeline.send(
                        f"new token listed on site: {name} ({symbol})\n{addr}",
                        level="info")
                    continue
                token_line = f"\ntoken: {name} ({symbol})" if token else ""
                headline = ("*** CLOCKIN *** IN FRONTEND BUNDLE"
                            if token else "NEW CONTRACT IN FRONTEND")
                self.pipeline.send(
                    f"{headline}\n{addr}{token_line}\n"
                    f"contract: {config.BLOCKSCOUT_ADDRESS_URL.format(addr=addr)}\n"
                    f"site: {config.LAUNCHER_PAGE_URL}\n"
                    "Possible factory / launch infra — appeared in the site "
                    "bundle before being announced.",
                    level="loud", code_lines=[addr],
                    # Only a CLOCKIN hit is a launch; a bare contract is recon.
                    category="launch" if token else "recon")
        self.state.save_if_dirty()

    def _check_launcher_factory(self, blobs: List[str]) -> None:
        """Detect the launcher factory address appearing in the site config.

        The bundle currently ships `launcherFactory:""`. When the team sets
        NEXT_PUBLIC_LAUNCHPAD_FACTORY and redeploys, the literal address is
        baked into the JS. That is the launcher factory by the site's own
        declaration — the strongest provenance available short of the API —
        so it is armed immediately and every launch through it is caught
        from its own logs.
        """
        for blob in blobs:
            for pattern in FACTORY_PATTERNS:
                match = pattern.search(blob)
                if not match:
                    continue
                factory = match.group(1).lower()
                if factory in config.BORING_ADDRESSES:
                    continue
                if self.state.is_known_launcher_factory(factory):
                    return
                start_block = 0
                block_match = FACTORY_START_BLOCK_RE.search(blob)
                if block_match:
                    start_block = int(block_match.group(1))
                self.state.add_candidate_factory(
                    factory, start_block, source="frontend_config")
                self.state.mark_dirty()
                self.state.save_if_dirty()
                log.warning("launcher factory published in bundle: %s", factory)
                self.pipeline.send(
                    f"LAUNCHER FACTORY PUBLISHED ON SITE\n{factory}\n"
                    f"start block: {start_block or 'not stated'}\n"
                    f"contract: "
                    f"{config.BLOCKSCOUT_ADDRESS_URL.format(addr=factory)}\n"
                    "The site now declares this as the Stonk Launcher "
                    "factory. Now watching all of its logs — launches "
                    "through it will alert from chain, ahead of the API.",
                    level="loud", code_lines=[factory], category="launch")
                return

    def interval(self, now=None) -> float:
        """Hourly normally, but tightened near T0: the factory address can
        be published any time in the run-up, and an hour of latency on that
        would waste the whole advantage."""
        now = now or datetime.now(timezone.utc)
        dt = config.seconds_to_t0(now, config.get_t0())
        if 3600 >= dt > -2 * 3600:
            return 60.0
        if 24 * 3600 >= dt > 3600:
            return 300.0
        return config.FRONTEND_POLL_INTERVAL
