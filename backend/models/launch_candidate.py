"""
LaunchCandidate — a token (existing or brand-new) that has been surfaced
by any of the launch-detection modules.

Signals are logged separately to `launch_signals` and aggregated into the
composite_score on this table. alert_tier (S/A/B/C) drives whether the
candidate fires a Telegram alert — see backend/detection/launch_scorer.py.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB

from backend.database import Base


class LaunchCandidate(Base):
    __tablename__ = "launch_candidates"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Unique key per (chain, contract_address). On Solana the "address" is
    # base58 up to 44 chars; on EVM it is 42. We use a wide VARCHAR to fit.
    chain = Column(String(10), default="bsc", nullable=False)
    contract_address = Column(String(64), nullable=False)

    deployer_address = Column(String(64))
    deploy_tx_hash = Column(String(80))
    deploy_block = Column(Integer)
    deployed_at = Column(DateTime)

    # First time any module surfaced this candidate
    detected_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_signal_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Aggregated scoring
    composite_score = Column(Integer, default=0)
    alert_tier = Column(String(4))  # 'S' | 'A' | 'B' | 'C'
    signal_summary = Column(JSONB)  # { module_name: confidence }

    # Alert state
    alert_fired = Column(Boolean, default=False)
    alert_fired_at = Column(DateTime)
    alert_tier_fired = Column(String(4))

    # Optional enrichment
    token_symbol = Column(String(64))
    token_name = Column(String(255))
    initial_lp_usd = Column(Numeric(20, 2))
    initial_mcap = Column(Numeric(20, 2))

    status = Column(String(20), default="watching")  # watching | alerted | expired
    notes = Column(Text)

    __table_args__ = (
        Index("idx_launch_candidate_chain_addr", "chain", "contract_address", unique=True),
        Index("idx_launch_candidate_tier", "alert_tier"),
        Index("idx_launch_candidate_status", "status"),
    )
