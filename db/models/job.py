"""Job model."""

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, List
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

if TYPE_CHECKING:
    from db.models.collection import Collection
    from db.models.lead import Lead


class JobStatus(str, Enum):
    """Job status enum."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


class Job(Base):
    """Job model for tracking scraping jobs."""
    
    __tablename__ = "jobs"
    
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        default=JobStatus.PENDING.value,
        nullable=False,
        index=True,
    )
    
    # Search parameters
    niche: Mapped[str | None] = mapped_column(String(255), nullable=True)
    keywords: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    areas: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    max_results: Mapped[int] = mapped_column(Integer, default=50)
    concurrency: Mapped[int] = mapped_column(Integer, default=3)
    priority: Mapped[int] = mapped_column(Integer, default=5)
    
    # Progress tracking
    total_keywords: Mapped[int] = mapped_column(Integer, default=0)
    processed_keywords: Mapped[int] = mapped_column(Integer, default=0)
    total_leads_found: Mapped[int] = mapped_column(Integer, default=0)
    total_leads_enriched: Mapped[int] = mapped_column(Integer, default=0)
    
    # Timestamps
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    
    # Error info
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    # Relationships
    collections: Mapped[List["Collection"]] = relationship(
        "Collection",
        back_populates="job",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    leads: Mapped[List["Lead"]] = relationship(
        "Lead",
        back_populates="job",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
