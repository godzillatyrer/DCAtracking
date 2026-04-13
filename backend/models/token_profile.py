from datetime import datetime
from sqlalchemy import Column, Integer, String, Numeric, DateTime, Boolean, ForeignKey
from sqlalchemy.dialects.postgresql import ARRAY
from backend.database import Base


class TokenProfile(Base):
    __tablename__ = "token_profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    contract_address = Column(
        String(42), ForeignKey("flagged_tokens.contract_address"), unique=True, nullable=False
    )
    max_supply = Column(Numeric(30, 0))
    circulating_supply = Column(Numeric(30, 0))
    float_pct = Column(Numeric(5, 2))
    token_age_days = Column(Integer)
    top_10_holder_pct = Column(Numeric(5, 2))
    top_1_holder_pct = Column(Numeric(5, 2))
    deployer_address = Column(String(42))
    is_contract_verified = Column(Boolean)
    has_proxy = Column(Boolean)
    has_mint_function = Column(Boolean)
    has_pause_function = Column(Boolean)
    has_blacklist_function = Column(Boolean)
    narrative_tags = Column(ARRAY(String))
    binance_alpha = Column(Boolean, default=False)
    binance_futures = Column(Boolean, default=False)
    exchange_listings = Column(ARRAY(String))
    coingecko_id = Column(String(100))
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
