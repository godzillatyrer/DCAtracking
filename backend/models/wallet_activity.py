from datetime import datetime
from sqlalchemy import Column, Integer, String, Numeric, DateTime, Boolean, BigInteger, ForeignKey, Index
from backend.database import Base


class WalletActivity(Base):
    __tablename__ = "wallet_activity"

    id = Column(Integer, primary_key=True, autoincrement=True)
    wallet_address = Column(
        String(42), ForeignKey("known_wallets.wallet_address"), nullable=False
    )
    activity_type = Column(String(50), nullable=False)
    # buy, sell, transfer_in, transfer_out, exchange_deposit,
    # exchange_withdrawal, new_token_accumulation, gas_funding
    token_contract = Column(String(42))
    token_symbol = Column(String(50))
    amount = Column(Numeric(30, 10))
    value_usd = Column(Numeric(20, 2))
    counterparty = Column(String(42))
    counterparty_label = Column(String(255))
    tx_hash = Column(String(66))
    block_number = Column(BigInteger)
    detected_at = Column(DateTime, default=datetime.utcnow)
    is_new_token = Column(Boolean, default=False)
    flagged = Column(Boolean, default=False)

    __table_args__ = (
        Index("idx_wallet_activity_wallet", "wallet_address"),
        Index("idx_wallet_activity_token", "token_contract"),
        Index("idx_wallet_activity_flagged", "flagged"),
    )
