"""Lead model (enriched leads)."""

from uuid import UUID, uuid4

from sqlalchemy import JSON, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base


class Lead(Base):
    """Enriched lead with contact information."""
    
    __tablename__ = "leads"
    
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
    collection_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("collections.id", ondelete="SET NULL"),
        nullable=True,
    )
    
    # Business info
    company_name: Mapped[str] = mapped_column(String(500), nullable=False)
    niche: Mapped[str | None] = mapped_column(String(255), nullable=True)
    website: Mapped[str | None] = mapped_column(String(500), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    # Contact info (enriched)
    emails: Mapped[list[str]] = mapped_column(JSON, default=list)
    phones: Mapped[list[str]] = mapped_column(JSON, default=list)
    whatsapp_numbers: Mapped[list[str]] = mapped_column(JSON, default=list)
    social_links: Mapped[list[str]] = mapped_column(JSON, default=list)
    
    # Enrichment metadata
    pages_crawled: Mapped[int] = mapped_column(Integer, default=0)
    email_count: Mapped[int] = mapped_column(Integer, default=0)
    phone_count: Mapped[int] = mapped_column(Integer, default=0)
    
    # Raw enrichment data
    enrichment_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    
    # Relationships
    job: Mapped["Job"] = relationship("Job", back_populates="leads")
    collection: Mapped["Collection | None"] = relationship("Collection")
