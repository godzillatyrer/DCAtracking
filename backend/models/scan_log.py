from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Index
from backend.database import Base


class ScanLog(Base):
    """Logs every scanner run — what it did, what it found, even if nothing triggered."""
    __tablename__ = "scan_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_name = Column(String(50), nullable=False)  # volume_scanner, profile_checker, etc.
    status = Column(String(20), default="success")  # success, error, skipped
    tokens_checked = Column(Integer, default=0)
    tokens_flagged = Column(Integer, default=0)
    details = Column(Text)  # human-readable summary of what happened
    error_message = Column(Text)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime)
    duration_seconds = Column(Integer)

    __table_args__ = (
        Index("idx_scan_logs_job", "job_name"),
        Index("idx_scan_logs_started", started_at.desc()),
    )
