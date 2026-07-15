"""Pydantic schemas for API request/response models."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class JobCreate(BaseModel):
    """Job creation schema."""

    name: str = Field(default="", min_length=1, max_length=255)
    niche: str | None = Field(default=None, max_length=255)
    keywords: list[str] = Field(default_factory=list)
    areas: list[str] = Field(default_factory=list)
    max_results: int = Field(default=50, ge=1, le=10000)
    concurrency: int = Field(default=3, ge=1, le=10)
    organization_id: str | None = Field(default=None, max_length=255)

    @model_validator(mode="before")
    @classmethod
    def transform_frontend_payload(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Map single 'keyword' to 'keywords' list
            if "keyword" in data and ("keywords" not in data or not data["keywords"]):
                data["keywords"] = [data["keyword"]]

            # Map single 'location' to 'areas' list
            if "location" in data and ("areas" not in data or not data["areas"]):
                if data["location"]:
                    data["areas"] = [data["location"]]
                else:
                    data["areas"] = ["India"] # default location if not specified

            # Generate default name if not provided
            if "name" not in data or not data["name"]:
                keyword = data.get("keyword") or (data.get("keywords")[0] if data.get("keywords") else "Scraping Job")
                location = data.get("location") or (data.get("areas")[0] if data.get("areas") else "All Locations")
                data["name"] = f"{keyword} {location}"

            # Ensure concurrency is present
            if "concurrency" not in data:
                data["concurrency"] = 3
        return data

    @field_validator("keywords")
    @classmethod
    def validate_keywords(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("At least one keyword is required")
        new_keywords = []
        for item in v:
            if "," in item:
                new_keywords.extend([x.strip() for x in item.split(",") if x.strip()])
            else:
                new_keywords.append(item.strip())
        final_keywords = [k for k in new_keywords if k]
        if not final_keywords:
            raise ValueError("At least one keyword is required")
        return final_keywords

    @field_validator("areas")
    @classmethod
    def validate_areas(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("At least one area is required")
        new_areas = []
        for item in v:
            if "," in item:
                new_areas.extend([x.strip() for x in item.split(",") if x.strip()])
            else:
                new_areas.append(item.strip())
        final_areas = [a for a in new_areas if a]
        if not final_areas:
            raise ValueError("At least one area is required")
        return final_areas


class JobResponse(BaseModel):
    """Job response schema."""

    id: UUID
    name: str
    status: str
    keywords: list[str]
    areas: list[str]
    max_results: int
    concurrency: int
    priority: int
    total_keywords: int
    processed_keywords: int
    total_leads_found: int
    total_leads_enriched: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error_message: str | None
    organization_id: str | None = None

    class Config:
        from_attributes = True


class JobListResponse(BaseModel):
    """Paginated job list response."""

    items: list[JobResponse]
    total: int
    page: int
    page_size: int


class LeadResponse(BaseModel):
    """Lead response schema."""

    id: UUID
    job_id: UUID
    company_name: str
    website: str | None
    emails: list[str]
    phones: list[str]
    whatsapp_numbers: list[str]
    social_links: list[str]
    pages_crawled: int
    email_count: int
    phone_count: int
    enrichment_data: dict[str, Any] | None
    created_at: datetime

    class Config:
        from_attributes = True


class CollectionResponse(BaseModel):
    """Collection (raw discovery) response schema."""

    id: UUID
    job_id: UUID
    company_name: str
    phone: str | None
    website: str | None
    keyword: str
    area: str
    rating: float | None
    review_count: int | None
    created_at: datetime

    class Config:
        from_attributes = True


class PipelineStatus(BaseModel):
    """Real-time pipeline status."""

    job_id: UUID
    status: str
    discovered: int
    enriched: int
    failed: int
    skipped: int
    queue_size: int
    elapsed_seconds: float
