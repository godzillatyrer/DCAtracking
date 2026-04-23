"""AppSetting — runtime-editable configuration store.

Every user-tunable threshold lives here instead of as a Python
constant. Modules read via backend.settings_cache.get(key, default).
The Settings frontend page is the canonical place to change values —
no need to touch env vars or redeploy.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, String, Text

from backend.database import Base


class AppSetting(Base):
    __tablename__ = "app_settings"

    key = Column(String(100), primary_key=True)
    value_json = Column(Text, nullable=False)   # JSON-encoded
    value_type = Column(String(20), nullable=False)  # int|float|bool|string
    category = Column(String(50), nullable=False)
    description = Column(Text)
    default_json = Column(Text)   # so "reset to default" works
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
