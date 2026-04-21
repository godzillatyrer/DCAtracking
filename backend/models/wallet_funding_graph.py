"""
WalletFundingEdge — directed graph of funding relationships.

Records an edge each time a tracked cabal wallet sends SOL to a fresh
recipient. Backing store for solana_graph_walk — lets us trace wallet
rotations.
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


class WalletFundingEdge(Base):
    __tablename__ = "wallet_funding_edges"

    id = Column(Integer, primary_key=True, autoincrement=True)

    chain = Column(String(10), default="solana", nullable=False)

    funder_address = Column(String(64), nullable=False)
    recipient_address = Column(String(64), nullable=False)

    amount_native = Column(Numeric(30, 10))  # SOL
    amount_usd = Column(Numeric(20, 2))
    # Solana base58 signatures are 87-88 chars; allow headroom.
    tx_hash = Column(String(128))
    block_number = Column(BigInteger)
    funded_at = Column(DateTime)

    # Snapshot of recipient state AT the moment of funding
    recipient_nonce_at_funding = Column(Integer)
    recipient_age_days_at_funding = Column(Integer)

    # Labels for the funder side — populated from Arkham/Nansen/our own db
    funder_label = Column(String(255))
    funder_role = Column(String(40))  # whale | vc | treasury | operator | unknown

    # Lifecycle: did the recipient deploy a contract after being funded?
    recipient_deployed = Column(Boolean, default=False)
    recipient_deploy_contract = Column(String(64))
    recipient_deploy_tx = Column(String(128))
    recipient_deploy_at = Column(DateTime)

    detected_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("idx_funding_recipient", "chain", "recipient_address"),
        Index("idx_funding_funder", "chain", "funder_address"),
        Index("idx_funding_tx", "tx_hash"),
        Index("idx_funding_deployed", "recipient_deployed"),
    )
