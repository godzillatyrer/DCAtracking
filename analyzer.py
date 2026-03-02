"""Detect high-conviction DCA accumulation on $20-50M tokens.

Only fires on genuinely large capital deployment — the kind of DCA
that protects a level or signals insider accumulation before a move.
Designed for leveraged long entries, not noise.
"""

import logging
from dataclasses import dataclass, field

import database
import config

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    token_mint: str
    signal_type: str   # mega_dca, coordinated_accumulation
    severity: str      # high, critical
    score: float       # 0-100
    details: dict = field(default_factory=dict)


def analyze_dca_order(order: dict, token_data: dict | None) -> list[Signal]:
    """Analyze a single DCA order. Only returns signals for serious capital.

    Filters:
    - Token mcap must be $20M-$50M
    - Single order must be >= $500K, OR
    - Multiple orders on same token totalling >= $500K in 24h
    """
    signals = []
    output_mint = order["output_mint"]
    in_amount_usd = order.get("in_amount_usd", 0) or 0

    if not token_data:
        return signals

    mcap = token_data.get("market_cap", 0)
    volume_24h = token_data.get("volume_24h", 0)
    liquidity = token_data.get("liquidity_usd", 0)

    # Hard filter: only $20M-$50M mcap tokens
    if mcap > 0 and (mcap < config.MIN_MARKET_CAP or mcap > config.MAX_MARKET_CAP):
        return signals

    # --- Signal 1: Mega DCA ---
    # Single order >= $500K on a $20-50M token. Someone is defending a level
    # or accumulating heavily before a catalyst.
    if in_amount_usd >= config.MIN_DCA_VALUE_USD:
        pct_of_mcap = (in_amount_usd / mcap * 100) if mcap > 0 else 0
        pct_of_volume = (in_amount_usd / volume_24h) if volume_24h > 0 else 999

        # Calculate DCA duration (how long the buy pressure lasts)
        cycle_freq = order.get("cycle_frequency_seconds", 0)
        total_cycles = order.get("total_cycles", 0)
        duration_hours = (cycle_freq * total_cycles / 3600) if cycle_freq and total_cycles else 0
        per_cycle_usd = in_amount_usd / total_cycles if total_cycles > 0 else in_amount_usd

        severity = "critical" if in_amount_usd >= 1_000_000 else "high"
        score = min(pct_of_mcap * 20, 100)  # 5% of mcap = score 100

        signals.append(Signal(
            token_mint=output_mint,
            signal_type="mega_dca",
            severity=severity,
            score=score,
            details={
                "dca_value_usd": round(in_amount_usd, 2),
                "pct_of_mcap": round(pct_of_mcap, 2),
                "pct_of_daily_volume": round(pct_of_volume * 100, 1),
                "duration_hours": round(duration_hours, 1),
                "per_cycle_usd": round(per_cycle_usd, 2),
                "total_cycles": total_cycles,
                "cycle_frequency_seconds": cycle_freq,
                "user_wallet": order["user_wallet"],
                "input_mint": order["input_mint"],
                "mcap": round(mcap, 2),
                "volume_24h": round(volume_24h, 2),
                "liquidity_usd": round(liquidity, 2),
            },
        ))

    # --- Signal 2: Coordinated Accumulation ---
    # Multiple wallets stacking DCA on the same token, total >= $500K in 24h.
    # This is the "group of insiders" pattern.
    window = config.COORDINATED_WINDOW_HOURS
    total_value = database.get_total_dca_value(output_mint, hours=window)

    if total_value >= config.COORDINATED_MIN_VALUE_USD:
        order_count = database.get_dca_order_count(output_mint, hours=window)
        unique_wallets = database.get_unique_dca_wallets(output_mint, hours=window)

        # Only fire if there are actually multiple participants
        if order_count >= 2 and unique_wallets >= 2:
            pct_of_mcap = (total_value / mcap * 100) if mcap > 0 else 0

            severity = "critical" if total_value >= 1_000_000 or unique_wallets >= 4 else "high"
            score = min(unique_wallets * 15 + pct_of_mcap * 10, 100)

            signals.append(Signal(
                token_mint=output_mint,
                signal_type="coordinated_accumulation",
                severity=severity,
                score=score,
                details={
                    "total_dca_value_usd": round(total_value, 2),
                    "order_count": order_count,
                    "unique_wallets": unique_wallets,
                    "window_hours": window,
                    "pct_of_mcap": round(pct_of_mcap, 2),
                    "mcap": round(mcap, 2),
                    "volume_24h": round(volume_24h, 2),
                    "liquidity_usd": round(liquidity, 2),
                },
            ))

    return signals


def compute_conviction_score(signals: list[Signal]) -> float:
    """Compute a conviction score for leveraged entry.

    This isn't a 'crime score' — it's how confident you should be
    that this DCA is protecting a level / accumulating for a move.
    """
    if not signals:
        return 0.0

    weights = {
        "mega_dca": 50,
        "coordinated_accumulation": 60,
    }

    severity_mult = {
        "high": 1.0,
        "critical": 1.5,
    }

    total = 0.0
    for signal in signals:
        weight = weights.get(signal.signal_type, 10)
        mult = severity_mult.get(signal.severity, 1.0)
        total += (signal.score / 100) * weight * mult

    # Both signals firing = very high conviction
    unique_types = len(set(s.signal_type for s in signals))
    if unique_types >= 2:
        total *= 1.4

    return min(total, 100.0)
