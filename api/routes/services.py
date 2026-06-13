"""Standalone API routes for Discovery and Enrichment services."""

from typing import Any
from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from api.dependencies import DbSession
from services.discovery import DiscoveryService
from services.enrichment import EnrichmentService

router = APIRouter()


class DiscoveryRequest(BaseModel):
    """Request schema for standalone discovery."""

    keyword: str = Field(..., description="Business category or keyword to search")
    area: str = Field(..., description="Target geographic area")
    max_results: int = Field(default=20, ge=1, le=500, description="Max results to retrieve")


class EnrichmentRequest(BaseModel):
    """Request schema for standalone enrichment."""

    website: str = Field(..., description="Website URL to scrape")
    name_hint: str = Field(default="", description="Optional business name hint")


@router.post("/discovery", response_model=list[dict[str, Any]], status_code=status.HTTP_200_OK)
async def run_discovery(
    data: DiscoveryRequest,
    db: DbSession,
) -> list[dict[str, Any]]:
    """Run Google Maps discovery standalone and return raw business list."""
    service = DiscoveryService(db)
    results = await service.discover_businesses(
        keyword=data.keyword,
        area=data.area,
        max_results=data.max_results,
    )
    return results


@router.post("/enrichment", response_model=dict[str, Any] | None, status_code=status.HTTP_200_OK)
async def run_enrichment(
    data: EnrichmentRequest,
    db: DbSession,
) -> dict[str, Any] | None:
    """Run website contact enrichment standalone and return scraped results."""
    service = EnrichmentService(db)
    result = await service.enrich_website(
        website=data.website,
        name_hint=data.name_hint,
    )
    return result
