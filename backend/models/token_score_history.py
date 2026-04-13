from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Index
from sqlalchemy.dialects.postgresql import JSONB
from backend.database import Base


class TokenScoreHistory(Base):
    __tablename__ = "token_score_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    contract_address = Column(String(42), nullable=False)
    score = Column(Integer)
    score_breakdown = Column(JSONB)
    recorded_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_score_history_contract", "contract_address"),
    )
