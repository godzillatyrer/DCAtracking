"""
PairCreation — every new DEX pair we detect on watched chains.

Seeds Modules 4 (Big Initial LP), 5 (Insider Early-Buyer Cluster),
9 (Known LP Provider). Rows are created by the pair watcher and
enriched lazily by downstream jobs.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
)

from backend.database import Base


class PairCreation(Base):
    __tablename__ = "pair_creations"

    id = Column(Integer, primary_key=True, autoincrement=True)

    chain = Column(String(10), default="bsc", nullable=False)
    dex = Column(String(40))  # pancakeswap_v2, pancakeswap_v3, raydium, orca, etc.

    pair_address = Column(String(64), nullable=False)
    base_token = Column(String(64), nullable=False)   # the new token
    quote_token = Column(String(64))                   # WBNB, USDT, etc.

    lp_provider = Column(String(64))  # address that added initial liquidity
    initial_lp_usd = Column(Numeric(20, 2))

    block_number = Column(BigInteger)
    created_at = Column(DateTime, default=datetime.utcnow)
    detected_at = Column(DateTime, default=datetime.utcnow)

    # First N buys aggregated so Module 5 can detect insider clustering
    first_buys_processed = Column(Boolean, default=False)
    insider_buyer_count = Column(Integer, default=0)

    __table_args__ = (
        Index("idx_pair_chain_addr", "chain", "pair_address", unique=True),
        Index("idx_pair_base", "base_token"),
        Index("idx_pair_created", "created_at"),
    )
