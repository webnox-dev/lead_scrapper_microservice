"""Collection model (raw leads from discovery)."""

from uuid import UUID, uuid4

from sqlalchemy import JSON, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base


class Collection(Base):
    """Raw lead collection from Google Maps discovery."""
    
    __tablename__ = "collections"
    
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    
    # Business info from Google Maps
    google_maps_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    company_name: Mapped[str] = mapped_column(String(500), nullable=False)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    website: Mapped[str | None] = mapped_column(String(500), nullable=True)
    rating: Mapped[float | None] = mapped_column(nullable=True)
    review_count: Mapped[int | None] = mapped_column(nullable=True)
    latitude: Mapped[float | None] = mapped_column(nullable=True)
    longitude: Mapped[float | None] = mapped_column(nullable=True)
    
    # Raw data storage
    raw_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    
    # Discovery metadata
    keyword: Mapped[str] = mapped_column(String(255), nullable=False)
    area: Mapped[str] = mapped_column(String(255), nullable=False)
    
    # Relationship
    job: Mapped["Job"] = relationship("Job", back_populates="collections")
