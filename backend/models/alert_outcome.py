"""AlertOutcome — post-hoc tracking of how an alert performed.

Created when an alert fires. An hourly tracker job polls DexScreener
and updates peak MC, drawdown, and a 15-minute "still holding?"
snapshot for convergence alerts. Closed after 24h.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)

from backend.database import Base


class AlertOutcome(Base):
    __tablename__ = "alert_outcomes"

    alert_id = Column(Integer, ForeignKey("alerts.id"), primary_key=True)
    mint = Column(String(64), nullable=False)

    # Snapshot at fire time
    mc_at_alert = Column(Numeric(20, 2))
    liq_at_alert = Column(Numeric(20, 2))
    price_at_alert = Column(Numeric(30, 12))
    fired_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Running peak / current
    peak_mc_usd = Column(Numeric(20, 2))
    peak_at = Column(DateTime)
    current_mc_usd = Column(Numeric(20, 2))
    current_price = Column(Numeric(30, 12))
    last_polled_at = Column(DateTime)

    # Computed: peak_pct = peak/initial - 1. drawdown = current/peak - 1.
    peak_pct = Column(Numeric(10, 2))
    drawdown_pct = Column(Numeric(10, 2))

    # Lifecycle
    is_closed = Column(Boolean, default=False)

    # Real-time enrichment: 15-min "did the cabal still hold?" snapshot
    holders_check_done = Column(Boolean, default=False)
    holders_still_holding = Column(Integer)        # count
    holders_exited = Column(Integer)
    holders_summary = Column(Text)

    __table_args__ = (
        Index("idx_outcome_mint", "mint"),
        Index("idx_outcome_open", "is_closed"),
    )
