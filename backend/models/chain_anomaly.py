"""ChainAnomaly — generic on-chain anomaly event.

One row per detected event (Hyperliquid whale trade, future Solana
DEX whale swap, future EVM whale flow, etc.). Independent of the
Alert table: this is the raw event log; Alert is for Telegram dedup
and outcome tracking.

Many anomalies will be alerted on; some (low-priority, deduped, etc.)
will only show in the dashboard feed.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)

from backend.database import Base


class ChainAnomaly(Base):
    __tablename__ = "chain_anomalies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(30), nullable=False)        # hyperliquid | solana_dex | ...
    event_type = Column(String(50), nullable=False)    # whale_trade | whale_swap | ...
    coin = Column(String(40))                           # symbol
    side = Column(String(10))                           # long | short | buy | sell
    notional_usd = Column(Numeric(20, 2))
    px = Column(Numeric(30, 12))
    sz = Column(Numeric(30, 6))

    actor_address = Column(String(64))
    counterparty_address = Column(String(64))
    is_fresh_wallet = Column(Boolean, default=False)
    actor_history_count = Column(Integer)

    tx_hash = Column(String(128))
    extra_json = Column(Text)
    detected_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    is_alerted = Column(Boolean, default=False)
    alert_id = Column(Integer)

    __table_args__ = (
        Index("idx_chain_anom_source_tx", "source", "tx_hash", unique=True),
        Index("idx_chain_anom_detected", detected_at.desc()),
        Index("idx_chain_anom_coin", "coin"),
    )
