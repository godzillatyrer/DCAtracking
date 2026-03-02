"""Detect suspicious DCA accumulation patterns on low-cap tokens."""

import logging
from dataclasses import dataclass, field

import database
import config

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    """A detected signal for a token."""
    token_mint: str
    signal_type: str  # large_dca, dca_cluster, dca_on_dead_token, whale_dca
    severity: str     # low, medium, high, critical
    score: float      # 0-100
    details: dict = field(default_factory=dict)


def analyze_dca_order(order: dict, token_data: dict | None) -> list[Signal]:
    """Analyze a DCA order for suspicious patterns.

    Args:
        order: DCA order data from jupiter_dca
        token_data: Token info from token_data (can be None)

    Returns:
        List of signals detected.
    """
    signals = []
    output_mint = order["output_mint"]
    in_amount_usd = order.get("in_amount_usd", 0) or 0

    mcap = token_data.get("market_cap", 0) if token_data else 0
    volume_24h = token_data.get("volume_24h", 0) if token_data else 0

    # --- Signal 1: Large DCA relative to token's daily volume ---
    if in_amount_usd >= config.MIN_DCA_VALUE_USD:
        if volume_24h > 0 and in_amount_usd > volume_24h * 0.1:
            ratio = in_amount_usd / volume_24h
            signals.append(Signal(
                token_mint=output_mint,
                signal_type="large_dca",
                severity=_size_severity(ratio),
                score=min(ratio * 50, 100),
                details={
                    "dca_value_usd": round(in_amount_usd, 2),
                    "volume_24h": round(volume_24h, 2),
                    "size_vs_volume": round(ratio, 2),
                    "user_wallet": order["user_wallet"],
                    "total_cycles": order.get("total_cycles", 0),
                    "cycle_frequency_hours": order.get("cycle_frequency_seconds", 0) / 3600,
                },
            ))
        elif volume_24h == 0 and in_amount_usd >= config.MIN_DCA_VALUE_USD:
            signals.append(Signal(
                token_mint=output_mint,
                signal_type="large_dca",
                severity="critical",
                score=95,
                details={
                    "dca_value_usd": round(in_amount_usd, 2),
                    "volume_24h": 0,
                    "size_vs_volume": 999,
                    "user_wallet": order["user_wallet"],
                    "total_cycles": order.get("total_cycles", 0),
                    "cycle_frequency_hours": order.get("cycle_frequency_seconds", 0) / 3600,
                },
            ))

    # --- Signal 2: DCA Cluster (multiple orders targeting same token) ---
    recent_count = database.get_dca_order_count(
        output_mint, hours=config.DCA_CLUSTER_WINDOW_HOURS
    )
    if recent_count >= config.DCA_CLUSTER_MIN_ORDERS:
        total_value = database.get_total_dca_value(
            output_mint, hours=config.DCA_CLUSTER_WINDOW_HOURS
        )
        unique_wallets = database.get_unique_dca_wallets(
            output_mint, hours=config.DCA_CLUSTER_WINDOW_HOURS
        )

        severity = "medium"
        if recent_count >= 5 or unique_wallets >= 3:
            severity = "high"
        if recent_count >= 10 or (volume_24h > 0 and total_value > volume_24h * 0.5):
            severity = "critical"

        signals.append(Signal(
            token_mint=output_mint,
            signal_type="dca_cluster",
            severity=severity,
            score=min(recent_count * 15 + unique_wallets * 10, 100),
            details={
                "order_count": recent_count,
                "total_value_usd": round(total_value, 2),
                "unique_wallets": unique_wallets,
                "window_hours": config.DCA_CLUSTER_WINDOW_HOURS,
                "volume_24h": round(volume_24h, 2),
            },
        ))

    # --- Signal 3: DCA on Dead/Low-Volume Token ---
    avg_vol = database.get_avg_volume(output_mint, hours=72)
    if avg_vol is not None and avg_vol < 5000:
        if in_amount_usd >= 100:
            revival_ratio = in_amount_usd / max(avg_vol, 1)
            signals.append(Signal(
                token_mint=output_mint,
                signal_type="dca_on_dead_token",
                severity="high" if revival_ratio > 1 else "medium",
                score=min(revival_ratio * 30, 100),
                details={
                    "dca_value_usd": round(in_amount_usd, 2),
                    "avg_daily_volume": round(avg_vol, 2),
                    "revival_ratio": round(revival_ratio, 2),
                },
            ))

    # --- Signal 4: Whale DCA (very large single order) ---
    if in_amount_usd >= 5000:
        if in_amount_usd >= 50000:
            severity = "critical"
        elif in_amount_usd >= 10000:
            severity = "high"
        else:
            severity = "medium"

        signals.append(Signal(
            token_mint=output_mint,
            signal_type="whale_dca",
            severity=severity,
            score=min(in_amount_usd / 500, 100),
            details={
                "dca_value_usd": round(in_amount_usd, 2),
                "user_wallet": order["user_wallet"],
                "total_cycles": order.get("total_cycles", 0),
                "cycle_frequency_hours": order.get("cycle_frequency_seconds", 0) / 3600,
                "token_mcap": round(mcap, 2),
            },
        ))

    return signals


def compute_crime_score(signals: list[Signal]) -> float:
    """Compute an overall suspicion score from multiple signals.
    Higher = more likely a coordinated pump setup."""
    if not signals:
        return 0.0

    weights = {
        "large_dca": 25,
        "dca_cluster": 35,
        "dca_on_dead_token": 30,
        "whale_dca": 20,
    }

    severity_mult = {
        "low": 0.5,
        "medium": 1.0,
        "high": 1.5,
        "critical": 2.0,
    }

    total = 0.0
    for signal in signals:
        weight = weights.get(signal.signal_type, 10)
        mult = severity_mult.get(signal.severity, 1.0)
        total += (signal.score / 100) * weight * mult

    # Bonus for multiple signal types firing on the same token
    unique_types = len(set(s.signal_type for s in signals))
    if unique_types >= 3:
        total *= 1.5
    elif unique_types >= 2:
        total *= 1.2

    return min(total, 100.0)


def _size_severity(ratio: float) -> str:
    if ratio >= 1.0:
        return "critical"
    elif ratio >= 0.5:
        return "high"
    elif ratio >= 0.1:
        return "medium"
    return "low"
