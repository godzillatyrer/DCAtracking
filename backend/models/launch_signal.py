"""
LaunchSignal — append-only log of every signal hit for every detected
launch candidate. The launch_scorer aggregates these into the parent
LaunchCandidate's composite_score and alert_tier.

Each detection module writes rows here; it never writes alert_tier or
composite_score directly. That keeps the gating logic in ONE place
(backend/detection/launch_scorer.py).
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from backend.database import Base


# Canonical list of detection modules so the scorer knows every possible key.
SIGNAL_TYPES = (
    "golden_deployer",          # Module 1
    "labeled_entity_deploy",    # Module 2
    "whale_fresh_wallet",       # Module 3  (self-S-tier)
    "big_initial_lp",           # Module 4  (filter only)
    "insider_early_buyer",      # Module 5
    "same_operator_different_deployer",  # Module 6
    "bytecode_fingerprint",     # Module 7
    "portfolio_size_gated",     # Module 8  (filter only)
    "known_lp_provider",        # Module 9
    "cross_chain_entity",       # Module 10
    "burner_sweeping",          # Module 11
    "launchpad_allocation",     # Module 12
    "treasury_outflow",         # Module 13
)


# Per-signal confidence tier. The scorer maps tiers to points and uses
# the number of distinct HIGH/MEDIUM signals to pick the alert tier.
TIER_HIGH = "high"
TIER_MEDIUM = "medium"
TIER_FILTER = "filter"   # never triggers alone; only augments score
TIER_SELF_S = "self_s"   # single signal sufficient for S-tier alert


class LaunchSignal(Base):
    __tablename__ = "launch_signals"

    id = Column(Integer, primary_key=True, autoincrement=True)

    chain = Column(String(10), default="bsc", nullable=False)
    contract_address = Column(String(64), nullable=False)

    signal_type = Column(String(60), nullable=False)
    signal_tier = Column(String(10), nullable=False)  # high | medium | filter | self_s
    confidence = Column(Integer, default=50)  # 0-100, module's own assessment
    points = Column(Integer, default=0)       # points contributed to composite

    # Free-form evidence from the module — e.g. the matched deployer address,
    # the gas-funder, the matching bytecode hash, etc. Displayed in the
    # dashboard + included in the AI briefing.
    evidence = Column(JSONB)
    description = Column(Text)

    detected_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("idx_launch_signal_chain_addr", "chain", "contract_address"),
        Index("idx_launch_signal_type", "signal_type"),
        Index("idx_launch_signal_detected", "detected_at"),
    )
