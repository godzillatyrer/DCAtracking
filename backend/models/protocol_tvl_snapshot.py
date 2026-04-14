"""
ProtocolTVLSnapshot — rolling TVL history from DeFi Llama.

Used by Module 14 (Exploit Detection) to spot sudden drains: if TVL
drops >30% in <15 minutes for a protocol that was worth >$50M, we flag
it as an exploit candidate.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, Index, Integer, Numeric, String
from backend.database import Base


class ProtocolTVLSnapshot(Base):
    __tablename__ = "protocol_tvl_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)

    protocol_slug = Column(String(100), nullable=False)  # DeFi Llama slug
    protocol_name = Column(String(255))
    chain = Column(String(40))

    tvl_usd = Column(Numeric(20, 2))
    native_token_symbol = Column(String(50))
    native_token_mcap = Column(Numeric(20, 2))

    snapshot_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("idx_tvl_slug_time", "protocol_slug", "snapshot_at"),
    )
