"""CexAddress — known Solana CEX deposit wallet addresses.

Seeded from data/cex_addresses.json at boot; users can add/remove
via the Settings page. When a tracked wallet sends SOL to one of
these, the activity tracker logs a 'cex_outflow' row so the UI /
alerts can show "cabal is cashing out."
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Index, String

from backend.database import Base


class CexAddress(Base):
    __tablename__ = "cex_addresses"

    address = Column(String(64), primary_key=True)
    name = Column(String(100), nullable=False)       # "Binance 1", "Coinbase 2", ...
    exchange = Column(String(50))                     # "binance" | "coinbase" | ...
    is_active = Column(Boolean, default=True)
    added_at = Column(DateTime, default=datetime.utcnow)
    notes = Column(String(255))

    __table_args__ = (
        Index("idx_cex_active", "is_active"),
    )
