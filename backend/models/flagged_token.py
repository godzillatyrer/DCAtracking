from datetime import datetime
from sqlalchemy import Column, Integer, String, Numeric, DateTime, Boolean, Index
from backend.database import Base


class FlaggedToken(Base):
    __tablename__ = "flagged_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    contract_address = Column(String(42), unique=True, nullable=False, index=True)
    token_name = Column(String(255))
    token_symbol = Column(String(50))
    chain = Column(String(20), default="bsc")
    pair_address = Column(String(42))
    dex_url = Column(String)
    price_usd = Column(Numeric(20, 10))
    volume_24h = Column(Numeric(20, 2))
    volume_change_pct = Column(Numeric(10, 2))
    buyers_24h = Column(Integer)
    buyers_change_pct = Column(Numeric(10, 2))
    liquidity_usd = Column(Numeric(20, 2))
    market_cap = Column(Numeric(20, 2))
    fdv = Column(Numeric(20, 2))
    pair_created_at = Column(DateTime)
    first_flagged_at = Column(DateTime, default=datetime.utcnow)
    last_seen_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String(20), default="raw")  # raw, candidate, watchlist, alerted, expired
    removed = Column(Boolean, default=False)

    __table_args__ = (
        Index("idx_flagged_status", "status"),
    )
