"""Volume anomaly detection and DCA pattern recognition."""

import logging
import time
from dataclasses import dataclass, field

import database
import config

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    """A detected signal for a token."""
    token_address: str
    chain: str
    symbol: str
    name: str
    signal_type: str  # volume_spike, buy_pressure, dead_revival, accumulation
    severity: str  # low, medium, high, critical
    score: float  # 0-100 composite score
    details: dict = field(default_factory=dict)


def analyze_token(data: dict) -> list[Signal]:
    """Analyze a single token's data for suspicious activity.
    Returns a list of signals detected."""

    signals = []
    token_addr = data["token_address"]
    chain = data["chain"]

    # Store snapshot for future comparisons
    snapshot = {**data, "timestamp": time.time()}
    database.save_snapshot(snapshot)

    # --- Signal 1: Volume Spike ---
    # Compare current 1h volume to historical average
    hist_vol = database.get_historical_avg_volume(token_addr, chain, hours=48)
    if hist_vol and hist_vol > 0 and data["volume_1h"] > 0:
        vol_ratio = data["volume_1h"] / hist_vol
        if vol_ratio >= config.VOLUME_SPIKE_MULTIPLIER:
            severity = _volume_severity(vol_ratio)
            signals.append(Signal(
                token_address=token_addr,
                chain=chain,
                symbol=data["token_symbol"],
                name=data["token_name"],
                signal_type="volume_spike",
                severity=severity,
                score=min(vol_ratio * 10, 100),
                details={
                    "current_1h_volume": data["volume_1h"],
                    "avg_1h_volume": round(hist_vol, 2),
                    "volume_ratio": round(vol_ratio, 1),
                    "volume_24h": data["volume_24h"],
                },
            ))

    # --- Signal 2: Buy Pressure (DCA-like accumulation) ---
    # When buys heavily outweigh sells, someone is accumulating
    buys_1h = data["buys_1h"]
    sells_1h = data["sells_1h"]
    total_txns_1h = buys_1h + sells_1h

    if total_txns_1h >= 5:  # Need minimum activity
        buy_ratio = buys_1h / total_txns_1h if total_txns_1h > 0 else 0

        # Also check 6h window for sustained pressure
        buys_6h = data["buys_6h"]
        sells_6h = data["sells_6h"]
        total_txns_6h = buys_6h + sells_6h
        buy_ratio_6h = buys_6h / total_txns_6h if total_txns_6h > 0 else 0

        # Strong buy pressure: >75% buys in both 1h and 6h windows
        if buy_ratio >= 0.75 and buy_ratio_6h >= 0.70:
            severity = "high" if buy_ratio >= 0.85 else "medium"
            signals.append(Signal(
                token_address=token_addr,
                chain=chain,
                symbol=data["token_symbol"],
                name=data["token_name"],
                signal_type="buy_pressure",
                severity=severity,
                score=buy_ratio * 100,
                details={
                    "buy_ratio_1h": round(buy_ratio, 2),
                    "buy_ratio_6h": round(buy_ratio_6h, 2),
                    "buys_1h": buys_1h,
                    "sells_1h": sells_1h,
                    "buys_6h": buys_6h,
                    "sells_6h": sells_6h,
                },
            ))

    # --- Signal 3: Dead Token Revival ---
    # Token had very low historical volume but suddenly active
    hist_avg = database.get_historical_avg_volume(token_addr, chain, hours=72)
    if hist_avg is not None and hist_avg < config.MIN_VOLUME_USD:
        # Was basically dead, now showing life
        if data["volume_1h"] >= config.MIN_VOLUME_USD * 5:
            revival_ratio = data["volume_1h"] / max(hist_avg, 1)
            signals.append(Signal(
                token_address=token_addr,
                chain=chain,
                symbol=data["token_symbol"],
                name=data["token_name"],
                signal_type="dead_revival",
                severity="high",
                score=min(revival_ratio * 5, 100),
                details={
                    "prev_avg_volume": round(hist_avg, 2),
                    "current_1h_volume": data["volume_1h"],
                    "revival_ratio": round(revival_ratio, 1),
                },
            ))

    # --- Signal 4: Accumulation Without Price Pump (stealth buying) ---
    # Heavy buying but price hasn't moved much yet = pre-pump accumulation
    if data["volume_1h"] >= config.MIN_VOLUME_USD * 3:
        price_change_1h = abs(data["price_change_1h"])
        price_change_6h = abs(data["price_change_6h"])

        # Significant volume but price barely moved (within +-5%)
        if price_change_1h < 5 and data["buys_1h"] > data["sells_1h"] * 1.5:
            hist_buys = database.get_historical_avg_buys(token_addr, chain, hours=48)
            if hist_buys and hist_buys > 0:
                buy_spike = data["buys_1h"] / hist_buys
                if buy_spike >= 3:
                    signals.append(Signal(
                        token_address=token_addr,
                        chain=chain,
                        symbol=data["token_symbol"],
                        name=data["token_name"],
                        signal_type="accumulation",
                        severity="critical",
                        score=min(buy_spike * 15, 100),
                        details={
                            "buy_spike_ratio": round(buy_spike, 1),
                            "price_change_1h": data["price_change_1h"],
                            "price_change_6h": data["price_change_6h"],
                            "buys_1h": data["buys_1h"],
                            "avg_buys_1h": round(hist_buys, 1),
                            "volume_1h": data["volume_1h"],
                        },
                    ))

    return signals


def compute_crime_score(signals: list[Signal]) -> float:
    """Compute an overall 'crime score' from multiple signals.
    Higher score = more likely to be a coordinated pump setup.

    Crime score combines:
    - Volume anomalies
    - Buy pressure patterns
    - Dead token revival
    - Stealth accumulation
    """
    if not signals:
        return 0.0

    # Weight different signal types
    weights = {
        "volume_spike": 20,
        "buy_pressure": 25,
        "dead_revival": 30,
        "accumulation": 35,
    }

    severity_multiplier = {
        "low": 0.5,
        "medium": 1.0,
        "high": 1.5,
        "critical": 2.0,
    }

    total = 0.0
    for signal in signals:
        weight = weights.get(signal.signal_type, 10)
        mult = severity_multiplier.get(signal.severity, 1.0)
        total += (signal.score / 100) * weight * mult

    # Bonus for multiple signal types (compound suspicion)
    unique_types = len(set(s.signal_type for s in signals))
    if unique_types >= 3:
        total *= 1.5
    elif unique_types >= 2:
        total *= 1.2

    return min(total, 100.0)


def _volume_severity(ratio: float) -> str:
    if ratio >= 20:
        return "critical"
    elif ratio >= 10:
        return "high"
    elif ratio >= 5:
        return "medium"
    return "low"
