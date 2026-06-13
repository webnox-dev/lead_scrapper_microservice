"""Failed job log model."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class FailedJob(Base):
    """Log of failed job attempts for retry/debugging."""
    
    __tablename__ = "failed_jobs"
    
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    job_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=True,
        index=True,
    )
    
    # Job snapshot at failure
    job_name: Mapped[str] = mapped_column(String(255), nullable=False)
    job_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    
    # Failure details
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    stack_trace: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    # Retry tracking
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    
    # Resolution
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    
    # Composite indexes for efficient querying
    __table_args__ = (
        Index("ix_failed_jobs_job_resolved", "job_id", "resolved_at"),
        Index("ix_failed_jobs_retry_resolved", "next_retry_at", "resolved_at"),
    )
