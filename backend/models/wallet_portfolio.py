"""
WalletPortfolio — cached portfolio snapshot (USD total + top holdings)
used by Module 8 (Portfolio Size Gated Deploy).

Cached because portfolio lookups via Arkham / Nansen are rate-limited
and this is a filter-only signal — staleness of a few hours is fine.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, Index, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB

from backend.database import Base


class WalletPortfolio(Base):
    __tablename__ = "wallet_portfolios"

    id = Column(Integer, primary_key=True, autoincrement=True)

    chain = Column(String(10), default="bsc", nullable=False)
    wallet_address = Column(String(64), nullable=False)

    total_usd = Column(Numeric(20, 2))
    top_holdings = Column(JSONB)   # [{symbol, usd}, …]

    source = Column(String(20))    # arkham | nansen | computed
    updated_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_portfolio_addr", "chain", "wallet_address", unique=True),
    )
