"""Discovery service - Google Maps business discovery.

Simplified v2 — no Redis, no cache, no WebSocket, no dedupe fingerprinting.
Pure Playwright-based Google Maps scraping that writes directly to the DB.

Supports both batch mode (returns list) and streaming mode (pushes to queue/callback).
"""

import asyncio
from typing import Any, Callable, Awaitable

from sqlalchemy.ext.asyncio import AsyncSession

from core.browser_manager import get_browser_manager
from core.config import settings
from core.logging import get_logger

logger = get_logger(__name__)

# Type alias for the callback that receives each discovered business
LeadCallback = Callable[[dict[str, Any]], Awaitable[None]]


class DiscoveryService:
    """Google Maps discovery service."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.browser = get_browser_manager()
        self._db_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()

    async def discover_businesses(
        self,
        keyword: str,
        area: str,
        max_results: int = 50,
        on_lead_found: LeadCallback | None = None,
        seen_urls: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Discover businesses on Google Maps for a keyword + area.

        Args:
            keyword: Business type (e.g. "restaurants")
            area: Location (e.g. "Coimbatore")
            max_results: Maximum number of results to return
            on_lead_found: Optional async callback called immediately when
                           each business is extracted. Use this to feed a
                           pipeline queue so enrichment starts in parallel.
            seen_urls: Optional set of already scraped/processed Maps URLs.

        Returns:
            List of all business detail dicts found.
        """
        # An empty area means a global keyword search.
        search_term = f"{keyword} {area}".strip()
        logger.info("discovery_started", keyword=keyword, area=area)

        # Search Google Maps
        async with self.browser.page() as page:
            success = await self.browser.goto_maps_search(search_term, page, area=area)
            if not success:
                logger.warning("maps_search_failed", term=search_term)
                return []

            # Scroll and collect place URLs with a target count to stop early
            place_urls = await self.browser.scroll_maps_results(page, target_count=max_results)
            logger.info(
                "maps_urls_found",
                term=search_term,
                count=len(place_urls),
            )

        # Filter out already seen URLs to avoid duplicate details extraction
        if seen_urls:
            place_urls = [u for u in place_urls if u.split("?")[0].strip() not in seen_urls]

        # No longer limiting discovered leads; return all results found to get 100% of GMB leads.
        pass

        # Process businesses concurrently
        results: list[dict[str, Any]] = []
        semaphore = asyncio.Semaphore(settings.business_concurrency)

        async def process_one(url: str) -> dict[str, Any] | None:
            async with semaphore:
                outcome = await self._extract_business(url)
                if outcome is not None:
                    results.append(outcome)
                    if on_lead_found is not None:
                        await on_lead_found(outcome)
                return outcome

        tasks = [process_one(url) for url in place_urls]
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                logger.error("business_processing_exception", error=str(outcome))

        logger.info(
            "discovery_completed",
            keyword=keyword,
            area=area,
            total=len(results),
        )
        return results

    async def _extract_business(self, url: str) -> dict[str, Any] | None:
        """Extract a single business's details from its Maps page."""
        try:
            async with self.browser.page() as page:
                details = await self.browser.extract_business_details(page, url)

            if not details or not details.get("name"):
                return None

            details["maps_url"] = url
            print(f"🔍 Found: {details['name']}")
            return details

        except Exception as e:
            logger.warning("business_extraction_failed", url=url, error=str(e))
            return None

    def stop(self) -> None:
        """Signal to stop discovery."""
        self._stop_event.set()
