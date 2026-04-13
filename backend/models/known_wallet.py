from datetime import datetime
from sqlalchemy import Column, Integer, String, Numeric, DateTime, Boolean, Text, Index
from backend.database import Base


class KnownWallet(Base):
    __tablename__ = "known_wallets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    wallet_address = Column(String(42), unique=True, nullable=False, index=True)
    label = Column(String(255))
    associated_token = Column(String(50))
    associated_contract = Column(String(42))
    role = Column(String(50))  # deployer, accumulator, distributor, cluster_member
    funding_source = Column(String(42))
    is_active = Column(Boolean, default=True)
    total_profit_est = Column(Numeric(20, 2))
    first_seen_at = Column(DateTime)
    added_at = Column(DateTime, default=datetime.utcnow)
    notes = Column(Text)

    __table_args__ = (
        Index("idx_known_wallets_active", "is_active"),
    )
