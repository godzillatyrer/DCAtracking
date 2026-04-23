from datetime import datetime
from sqlalchemy import Column, Integer, String, Numeric, DateTime, Boolean, Text, Index
from backend.database import Base


class Alert(Base):
    """Fired notifications. Chain-agnostic; currently used for Solana cabal alerts."""

    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Token/mint this alert is about. Solana base58 mints are 32-44 chars.
    contract_address = Column(String(64), nullable=False)
    token_symbol = Column(String(50))

    alert_type = Column(String(50))  # cabal_convergence | sniper_solo | cabal_exit
    trigger_reason = Column(Text)
    telegram_sent = Column(Boolean, default=False)
    telegram_message_id = Column(Integer)
    fired_at = Column(DateTime, default=datetime.utcnow)

    # Snapshot at fire time (for outcome tracking)
    mc_at_alert = Column(Numeric(20, 2))
    price_at_alert = Column(Numeric(30, 12))

    # Legacy outcome summary fields (kept for compatibility; the real
    # per-alert outcome lives in alert_outcomes).
    outcome = Column(String(20))
    peak_pct_from_alert = Column(Numeric(10, 2))
    reviewed = Column(Boolean, default=False)
    review_notes = Column(Text)

    __table_args__ = (
        Index("idx_alerts_fired_at", fired_at.desc()),
    )
