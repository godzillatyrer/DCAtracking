"""WalletClassification — cached fresh/dormant flags for arbitrary wallets.

The freshie/dormant SWARM watchers classify random wallets that buy
trending coins (not just our tracked cabal). Classification is
expensive (1 Helius RPC per wallet), so we cache results in this
table with a TTL.

A wallet's "fresh" status changes slowly (it stops being fresh once
it does ~20+ txs over a week+), so 24h cache is a safe trade-off
between freshness and RPC cost.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    String,
)

from backend.database import Base


class WalletClassification(Base):
    __tablename__ = "wallet_classifications"

    wallet_address = Column(String(64), primary_key=True)
    chain = Column(String(10), default="solana", nullable=False)

    is_fresh = Column(Boolean, default=False)
    is_dormant = Column(Boolean, default=False)

    tx_count_observed = Column(Integer)
    first_seen_at = Column(DateTime)
    last_active_at = Column(DateTime)

    classified_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("idx_wclass_expires", "expires_at"),
        Index("idx_wclass_fresh", "is_fresh"),
        Index("idx_wclass_dormant", "is_dormant"),
    )
