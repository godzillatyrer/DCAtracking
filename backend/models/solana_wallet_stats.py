"""
SolanaWalletStats — rolled-up lifetime stats per tracked wallet.

Populated by backend.detection.wallet_stats_aggregator on a timer.
Feeds:
  - the Leaderboard page
  - the convergence confidence score (we weight by wallet quality,
    not just role)
  - solo alerts (top-ranked wallets buying a NEW mint)

`entity_id` groups wallets that share a funding source (anti-Sybil).
When five wallets were all bootstrapped from the same operator
address, they count as one entity in the convergence score.
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


class SolanaWalletStats(Base):
    __tablename__ = "solana_wallet_stats"

    wallet_address = Column(String(64), primary_key=True)

    # Trade counts
    tx_count = Column(Integer, default=0)
    buy_count = Column(Integer, default=0)
    sell_count = Column(Integer, default=0)
    mints_traded = Column(Integer, default=0)
    mints_closed = Column(Integer, default=0)

    # Aggregates (USD)
    total_buy_usd = Column(Numeric(20, 2), default=0)
    total_sell_usd = Column(Numeric(20, 2), default=0)
    net_profit_usd = Column(Numeric(20, 2), default=0)

    # Per-mint extremes
    best_mint = Column(String(64))
    best_mint_profit_usd = Column(Numeric(20, 2))
    worst_mint_profit_usd = Column(Numeric(20, 2))

    # Win/loss on closed positions
    win_count = Column(Integer, default=0)
    loss_count = Column(Integer, default=0)

    # Sniper profile — small entries, high exit multiples, consistent.
    avg_buy_size_usd = Column(Numeric(20, 2))
    avg_exit_multiplier = Column(Numeric(10, 2))
    is_sniper = Column(Boolean, default=False)

    # Decay — wallet with no new activity in WALLET_DORMANT_DAYS.
    is_dormant = Column(Boolean, default=False)

    # Freshness
    last_activity_at = Column(DateTime)

    # Composite rank
    confidence_score = Column(Numeric(10, 2), default=0)

    # Funder cluster
    entity_id = Column(BigInteger)
    entity_size = Column(Integer, default=1)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("idx_sol_stats_confidence", confidence_score.desc()),
        Index("idx_sol_stats_entity", "entity_id"),
        Index("idx_sol_stats_sniper", "is_sniper"),
        Index("idx_sol_stats_dormant", "is_dormant"),
    )
