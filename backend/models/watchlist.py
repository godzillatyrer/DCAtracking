from datetime import datetime
from sqlalchemy import Column, Integer, String, Numeric, DateTime, Boolean, ForeignKey, Text, Index
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from backend.database import Base


class Watchlist(Base):
    __tablename__ = "watchlist"

    id = Column(Integer, primary_key=True, autoincrement=True)
    contract_address = Column(
        String(42), ForeignKey("flagged_tokens.contract_address"), unique=True, nullable=False
    )
    current_score = Column(Integer, default=0)
    score_breakdown = Column(JSONB)
    cluster_detected = Column(Boolean, default=False)
    cluster_wallet_count = Column(Integer, default=0)
    cluster_funding_sources = Column(ARRAY(String))
    exchange_deposits_detected = Column(Boolean, default=False)
    exchange_deposit_volume = Column(Numeric(20, 2), default=0)
    social_signal_detected = Column(Boolean, default=False)
    social_mention_count = Column(Integer, default=0)
    ai_briefing = Column(Text)
    ai_contract_analysis = Column(Text)
    confidence_level = Column(String(10))  # HIGH, MEDIUM, LOW
    added_at = Column(DateTime, default=datetime.utcnow)
    last_scored_at = Column(DateTime, default=datetime.utcnow)
    alert_fired = Column(Boolean, default=False)
    alert_fired_at = Column(DateTime)
    price_at_alert = Column(Numeric(20, 10))
    outcome = Column(String(20))  # pumped, fizzled, unknown
    peak_price_after_alert = Column(Numeric(20, 10))
    peak_pct_gain = Column(Numeric(10, 2))
    notes = Column(Text)

    __table_args__ = (
        Index("idx_watchlist_score", current_score.desc()),
    )
