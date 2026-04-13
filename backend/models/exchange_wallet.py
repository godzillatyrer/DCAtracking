from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Boolean
from backend.database import Base


class ExchangeWallet(Base):
    __tablename__ = "exchange_wallets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    wallet_address = Column(String(42), unique=True, nullable=False)
    exchange_name = Column(String(100))
    wallet_type = Column(String(50))  # hot_wallet, deposit_aggregator
    chain = Column(String(20), default="bsc")
    verified = Column(Boolean, default=True)
    added_at = Column(DateTime, default=datetime.utcnow)
