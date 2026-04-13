from datetime import datetime
from sqlalchemy import Column, Integer, String, Numeric, DateTime, Boolean, Text, Index
from backend.database import Base


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    contract_address = Column(String(42), nullable=False)
    token_symbol = Column(String(50))
    score_at_alert = Column(Integer)
    alert_type = Column(String(50))  # score_threshold, wallet_tracker, exchange_flow
    trigger_reason = Column(Text)
    ai_briefing = Column(Text)
    price_at_alert = Column(Numeric(20, 10))
    market_cap_at_alert = Column(Numeric(20, 2))
    telegram_sent = Column(Boolean, default=False)
    telegram_message_id = Column(Integer)
    fired_at = Column(DateTime, default=datetime.utcnow)
    # Outcome tracking
    outcome = Column(String(20))  # pumped, fizzled, still_active
    peak_price = Column(Numeric(20, 10))
    peak_pct_from_alert = Column(Numeric(10, 2))
    time_to_peak_hours = Column(Integer)
    exit_price = Column(Numeric(20, 10))
    exit_pct = Column(Numeric(10, 2))
    reviewed = Column(Boolean, default=False)
    review_notes = Column(Text)

    __table_args__ = (
        Index("idx_alerts_fired_at", fired_at.desc()),
    )
