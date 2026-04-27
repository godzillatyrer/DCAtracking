"""MajorCoin — symbols to EXCLUDE from anomaly watchers.

User asked for "non-major" coins only. Seeded with the top 15-20
by mcap. Editable from the Settings page. Same admin pattern as
CexAddress.
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, String

from backend.database import Base


class MajorCoin(Base):
    __tablename__ = "major_coins"

    symbol = Column(String(20), primary_key=True)
    label = Column(String(100))
    excluded = Column(Boolean, default=True)
    added_at = Column(DateTime, default=datetime.utcnow)
