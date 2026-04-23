"""Alert Outcome Tracker.

Runs every 10 min. For every open AlertOutcome row (created when an
alert fires), poll DexScreener for the current MC + price and update:
  - peak_mc_usd + peak_at (if new peak)
  - current_mc_usd + current_price
  - peak_pct, drawdown_pct
  - 15-minute "are the buyers still holding?" snapshot (once)
  - is_closed=True once the alert is 24h old

Surfaces via /api/wallets/solana/outcomes to drive an alert-history
dashboard so you can see how your signal actually performed.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from backend.clients import dexscreener, helius
from backend.database import SessionLocal
from backend.models.alert import Alert
from backend.models.alert_outcome import AlertOutcome
from backend.models.solana_wallet_activity import SolanaWalletActivity

logger = logging.getLogger(__name__)

OUTCOME_LIFETIME_HOURS = 24
HOLDERS_CHECK_DELAY_MIN = 15


def _safe_dec(v) -> Decimal | None:
    if v is None or v == 0:
        return None
    try:
        return Decimal(str(round(float(v), 2)))
    except Exception:
        return None


async def _check_still_holding(db: Session, outcome: AlertOutcome, alert: Alert):
    """Snapshot of who's still holding vs exited 15 min after the alert.
    Uses activity data — who among the original alerted-cluster wallets
    has an spl_sell row on this mint since the alert fired?"""
    try:
        # Find wallets that bought this mint around the alert time
        buyer_rows = (
            db.query(SolanaWalletActivity.wallet_address)
            .filter(
                SolanaWalletActivity.token_mint == outcome.mint,
                SolanaWalletActivity.activity_type == "spl_buy",
                SolanaWalletActivity.detected_at >= alert.fired_at - timedelta(minutes=60),
                SolanaWalletActivity.detected_at <= alert.fired_at + timedelta(minutes=5),
            )
            .distinct()
            .all()
        )
        buyer_addrs = {r[0] for r in buyer_rows}
        if not buyer_addrs:
            outcome.holders_check_done = True
            outcome.holders_summary = "no buyer wallets found"
            return

        # Sells by any of those wallets since the alert
        exited_rows = (
            db.query(SolanaWalletActivity.wallet_address)
            .filter(
                SolanaWalletActivity.token_mint == outcome.mint,
                SolanaWalletActivity.activity_type == "spl_sell",
                SolanaWalletActivity.wallet_address.in_(buyer_addrs),
                SolanaWalletActivity.detected_at >= alert.fired_at,
            )
            .distinct()
            .all()
        )
        exited = {r[0] for r in exited_rows}
        still = buyer_addrs - exited
        outcome.holders_still_holding = len(still)
        outcome.holders_exited = len(exited)
        outcome.holders_check_done = True
        outcome.holders_summary = (
            f"{len(still)}/{len(buyer_addrs)} still holding 15m after alert"
        )
    except Exception as e:
        logger.error(f"holders check failed for alert {alert.id}: {e}")


async def run_alert_outcome_tracker():
    db = SessionLocal()
    updated = 0
    holders_checks = 0
    closed = 0
    try:
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=OUTCOME_LIFETIME_HOURS)
        # Pull every open outcome + its parent alert
        rows = (
            db.query(AlertOutcome, Alert)
            .join(Alert, Alert.id == AlertOutcome.alert_id)
            .filter(AlertOutcome.is_closed.is_(False))
            .all()
        )
        if not rows:
            return {"updated": 0, "closed": 0}

        # Batch market lookups
        mints = list({o.mint for (o, _) in rows})
        infos = await asyncio.gather(
            *[dexscreener.token_info(m) for m in mints],
            return_exceptions=True,
        )
        info_by_mint = {
            m: (i if not isinstance(i, Exception) else None)
            for m, i in zip(mints, infos)
        }

        for outcome, alert in rows:
            mkt = info_by_mint.get(outcome.mint)
            if mkt:
                mc = mkt.get("market_cap_usd") or 0
                price = mkt.get("price_usd") or 0
                if mc and mc > 0:
                    outcome.current_mc_usd = _safe_dec(mc)
                    outcome.current_price = _safe_dec(price)
                    prev_peak = float(outcome.peak_mc_usd or 0)
                    if mc > prev_peak:
                        outcome.peak_mc_usd = _safe_dec(mc)
                        outcome.peak_at = now
                    # Recompute ratios
                    initial = float(outcome.mc_at_alert or 0)
                    if initial > 0:
                        peak = float(outcome.peak_mc_usd or initial)
                        outcome.peak_pct = _safe_dec(
                            round((peak / initial - 1) * 100, 2)
                        )
                        outcome.drawdown_pct = _safe_dec(
                            round((mc / peak - 1) * 100, 2) if peak > 0 else 0
                        )
                outcome.last_polled_at = now
                updated += 1

            # 15-min holders snapshot (once, for convergence alerts)
            if (
                not outcome.holders_check_done
                and alert.alert_type == "cabal_convergence"
                and alert.fired_at
                and (now - alert.fired_at) >= timedelta(minutes=HOLDERS_CHECK_DELAY_MIN)
            ):
                await _check_still_holding(db, outcome, alert)
                holders_checks += 1

            # Close after 24h
            if alert.fired_at and (now - alert.fired_at) >= timedelta(hours=OUTCOME_LIFETIME_HOURS):
                outcome.is_closed = True
                closed += 1

        db.commit()
        logger.info(
            f"alert_outcome_tracker: updated {updated} outcomes, "
            f"{holders_checks} holders checks, closed {closed}"
        )
        return {
            "updated": updated, "holders_checked": holders_checks,
            "closed": closed,
        }
    finally:
        db.close()
