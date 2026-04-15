"""SolanaWalletActivity — activity log for tracked Solana wallets."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
)

from backend.database import Base


class SolanaWalletActivity(Base):
    __tablename__ = "solana_wallet_activity"

    id = Column(Integer, primary_key=True, autoincrement=True)
    wallet_address = Column(
        String(64),
        ForeignKey("solana_known_wallets.wallet_address"),
        nullable=False,
    )
    activity_type = Column(String(50), nullable=False)
    # transfer_in, transfer_out, spl_buy, spl_sell, new_token_accumulation,
    # mint_authority_deploy, sol_funding
    token_mint = Column(String(64))
    token_symbol = Column(String(64))
    amount = Column(Numeric(30, 10))
    value_usd = Column(Numeric(20, 2))
    counterparty = Column(String(64))
    counterparty_label = Column(String(255))
    signature = Column(String(96))   # Solana tx signature ~88 chars
    slot = Column(BigInteger)
    detected_at = Column(DateTime, default=datetime.utcnow)
    is_new_token = Column(Boolean, default=False)
    flagged = Column(Boolean, default=False)

    __table_args__ = (
        Index("idx_sol_wallet_activity_wallet", "wallet_address"),
        Index("idx_sol_wallet_activity_mint", "token_mint"),
        Index("idx_sol_wallet_activity_flagged", "flagged"),
    )
