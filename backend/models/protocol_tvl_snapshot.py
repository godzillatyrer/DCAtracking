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

    # protocol_slug: DeFi Llama slugs are usually <40 chars but a few are
    # long composites (e.g. forks). 255 leaves headroom indefinitely.
    protocol_slug = Column(String(255), nullable=False)
    protocol_name = Column(String(255))

    # chain: comma-joined list of chain names from DeFi Llama. Multi-chain
    # protocols (Ethereum,Plasma,Arbitrum,Base,Mantle,...) routinely
    # exceed 40 chars; 255 is plenty.
    chain = Column(String(255))

    tvl_usd = Column(Numeric(20, 2))
    native_token_symbol = Column(String(50))
    native_token_mcap = Column(Numeric(20, 2))

    snapshot_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("idx_tvl_slug_time", "protocol_slug", "snapshot_at"),
    )
