"""
Backtest Script — Test scoring against historical pump data.

Runs the scoring engine against the 5 confirmed pumps to validate
that the scoring weights would have detected them.

Usage:
    python scripts/backtest.py
"""

import sys
import os
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.scoring.scorer import calculate_score, SCORING_WEIGHTS
from backend.models.flagged_token import FlaggedToken
from backend.models.token_profile import TokenProfile
from backend.models.watchlist import Watchlist


# Historical data for confirmed pumps (pre-pump state)
CONFIRMED_PUMPS = [
    {
        "symbol": "RAVE",
        "name": "RaveDAO",
        "contract": "0x17205fab260a7a6383a81452ce6315a39370db97",
        "pump_pct": 2900,
        "crash_pct": None,  # Still active at time of analysis
        "flagged_data": {
            "volume_24h": Decimal("5000000"),
            "volume_change_pct": Decimal("800"),  # ~9x spike
            "market_cap": Decimal("2000000"),
            "price_usd": Decimal("0.05"),
        },
        "profile_data": {
            "float_pct": Decimal("24.8"),
            "top_10_holder_pct": Decimal("75.2"),
            "token_age_days": 180,
            "binance_alpha": True,
            "has_mint_function": False,
            "has_pause_function": False,
            "has_blacklist_function": False,
            "is_contract_verified": True,
            "narrative_tags": ["DeFi"],
        },
        "watchlist_data": {
            "cluster_detected": True,
            "cluster_wallet_count": 8,
            "exchange_deposits_detected": True,
            "social_signal_detected": True,
            "score_breakdown": {"known_operator_present": True},
        },
    },
    {
        "symbol": "SIREN",
        "name": "SirenCoin",
        "contract": "0x997a58129890bbda032231a52ed1ddc845fc18e1",
        "pump_pct": 13700,
        "crash_pct": 85,
        "flagged_data": {
            "volume_24h": Decimal("10000000"),
            "volume_change_pct": Decimal("2500"),  # ~26x spike
            "market_cap": Decimal("3000000"),
            "price_usd": Decimal("0.001"),
        },
        "profile_data": {
            "float_pct": Decimal("72.8"),
            "top_10_holder_pct": Decimal("88.0"),
            "token_age_days": 90,
            "binance_alpha": True,
            "has_mint_function": True,
            "has_pause_function": True,
            "has_blacklist_function": False,
            "is_contract_verified": True,
            "narrative_tags": ["AI"],
        },
        "watchlist_data": {
            "cluster_detected": True,
            "cluster_wallet_count": 15,
            "exchange_deposits_detected": True,
            "social_signal_detected": True,
            "score_breakdown": {"known_operator_present": True},
        },
    },
    {
        "symbol": "RIVER",
        "name": "River Protocol",
        "contract": "0xda7ad9dea9397cffddae2f8a052b82f1484252b3",
        "pump_pct": 5400,
        "crash_pct": 90,
        "flagged_data": {
            "volume_24h": Decimal("8000000"),
            "volume_change_pct": Decimal("1400"),  # ~15x spike
            "market_cap": Decimal("5000000"),
            "price_usd": Decimal("0.10"),
        },
        "profile_data": {
            "float_pct": Decimal("20.0"),
            "top_10_holder_pct": Decimal("94.0"),
            "token_age_days": 120,
            "binance_alpha": True,
            "has_mint_function": False,
            "has_pause_function": False,
            "has_blacklist_function": False,
            "is_contract_verified": True,
            "narrative_tags": ["DeFi", "Web3"],
        },
        "watchlist_data": {
            "cluster_detected": True,
            "cluster_wallet_count": 5,
            "exchange_deposits_detected": False,
            "social_signal_detected": False,
            "score_breakdown": {},
        },
    },
    {
        "symbol": "ARIA",
        "name": "AriaAI",
        "contract": "0x5d3a12c42e5372b2cc3264ab3cdcf660a1555238",
        "pump_pct": 2700,
        "crash_pct": None,
        "flagged_data": {
            "volume_24h": Decimal("3000000"),
            "volume_change_pct": Decimal("600"),  # ~7x
            "market_cap": Decimal("4000000"),
            "price_usd": Decimal("0.02"),
        },
        "profile_data": {
            "float_pct": Decimal("18.3"),
            "top_10_holder_pct": Decimal("81.7"),
            "token_age_days": 60,
            "binance_alpha": True,
            "has_mint_function": False,
            "has_pause_function": True,
            "has_blacklist_function": True,
            "is_contract_verified": True,
            "narrative_tags": ["AI"],
        },
        "watchlist_data": {
            "cluster_detected": True,
            "cluster_wallet_count": 10,
            "exchange_deposits_detected": True,
            "social_signal_detected": True,
            "score_breakdown": {"known_operator_present": True},
        },
    },
    {
        "symbol": "STO",
        "name": "StakeStone",
        "contract": "",
        "pump_pct": 1600,
        "crash_pct": 93,
        "flagged_data": {
            "volume_24h": Decimal("15000000"),
            "volume_change_pct": Decimal("1900"),  # ~20x
            "market_cap": Decimal("10000000"),
            "price_usd": Decimal("0.50"),
        },
        "profile_data": {
            "float_pct": Decimal("15.0"),
            "top_10_holder_pct": Decimal("70.0"),
            "token_age_days": 200,
            "binance_alpha": True,
            "has_mint_function": False,
            "has_pause_function": False,
            "has_blacklist_function": False,
            "is_contract_verified": True,
            "narrative_tags": ["DeFi"],
        },
        "watchlist_data": {
            "cluster_detected": False,
            "exchange_deposits_detected": True,  # Whale withdrew from Binance
            "social_signal_detected": True,
            "score_breakdown": {},
        },
    },
]


def create_mock_objects(pump_data: dict):
    """Create mock ORM objects from historical data."""
    flagged = FlaggedToken()
    flagged.contract_address = pump_data["contract"]
    flagged.token_name = pump_data["name"]
    flagged.token_symbol = pump_data["symbol"]
    for k, v in pump_data["flagged_data"].items():
        setattr(flagged, k, v)

    profile = TokenProfile()
    profile.contract_address = pump_data["contract"]
    for k, v in pump_data["profile_data"].items():
        setattr(profile, k, v)

    watchlist = Watchlist()
    watchlist.contract_address = pump_data["contract"]
    for k, v in pump_data["watchlist_data"].items():
        setattr(watchlist, k, v)

    return flagged, profile, watchlist


def run_backtest():
    """Run scoring backtest against all confirmed pumps."""
    print("=" * 70)
    print("BSC Pump Scanner — Backtest Results")
    print("=" * 70)
    print()

    all_scores = []
    alerts_triggered = 0

    for pump in CONFIRMED_PUMPS:
        flagged, profile, watchlist = create_mock_objects(pump)
        score, breakdown = calculate_score(flagged, profile, watchlist)
        all_scores.append(score)

        if score >= 70:
            alerts_triggered += 1
            verdict = "ALERT TRIGGERED"
            color = "HIGH"
        elif score >= 50:
            verdict = "WATCHLIST"
            color = "MEDIUM"
        else:
            verdict = "MISSED"
            color = "LOW"

        print(f"Token: {pump['symbol']} ({pump['name']})")
        print(f"  Actual pump: +{pump['pump_pct']}%", end="")
        if pump.get("crash_pct"):
            print(f"  |  Crash: -{pump['crash_pct']}%")
        else:
            print()
        print(f"  Score: {score}/100  |  Verdict: {verdict}  |  Confidence: {color}")
        print(f"  Signals detected:")
        for signal, points in sorted(breakdown.items(), key=lambda x: -x[1]):
            print(f"    + {signal}: {points}")
        print()

    # Summary
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  Tokens tested:     {len(CONFIRMED_PUMPS)}")
    print(f"  Alerts triggered:  {alerts_triggered}/{len(CONFIRMED_PUMPS)}")
    print(f"  Detection rate:    {alerts_triggered/len(CONFIRMED_PUMPS)*100:.0f}%")
    print(f"  Average score:     {sum(all_scores)/len(all_scores):.1f}/100")
    print(f"  Min score:         {min(all_scores)}/100")
    print(f"  Max score:         {max(all_scores)}/100")
    print()

    if alerts_triggered == len(CONFIRMED_PUMPS):
        print("  Result: ALL confirmed pumps would have triggered alerts.")
    else:
        missed = len(CONFIRMED_PUMPS) - alerts_triggered
        print(f"  Result: {missed} pump(s) would have been missed.")
        print("  Consider adjusting scoring weights.")

    print()


if __name__ == "__main__":
    run_backtest()
