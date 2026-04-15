"""
SolanaKnownWallet — Solana counterpart to KnownWallet.

Kept as a separate table because Solana addresses are base58 (32-44 chars)
not hex, and BSC's `known_wallets` column is String(42). Splitting the
domain avoids a destructive migration on the legacy table.

Roles mirror the BSC model: deployer / accumulator / whale / vc /
treasury / operator / golden_deployer.
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, Numeric, String, Text

from backend.database import Base


class SolanaKnownWallet(Base):
    __tablename__ = "solana_known_wallets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    wallet_address = Column(String(64), unique=True, nullable=False, index=True)
    label = Column(String(255))
    associated_token = Column(String(64))
    associated_mint = Column(String(64))
    role = Column(String(50))
    funding_source = Column(String(64))
    is_active = Column(Boolean, default=True)
    total_profit_est = Column(Numeric(20, 2))
    first_seen_at = Column(DateTime)
    added_at = Column(DateTime, default=datetime.utcnow)
    notes = Column(Text)

    __table_args__ = (
        Index("idx_sol_known_wallets_active", "is_active"),
    )
