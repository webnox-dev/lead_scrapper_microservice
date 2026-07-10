"""BrowserManager - singleton browser management with context and page pooling."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncGenerator
from urllib.parse import quote

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Route,
    async_playwright,
)

from core.config import settings
from core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class PagePoolEntry:
    """Page pool entry."""

    page: Page
    context_id: str
    in_use: bool = False


class BrowserManager:
    """Singleton browser manager with pooling."""

    _instance: "BrowserManager | None" = None
    _lock = asyncio.Lock()

    def __new__(cls) -> "BrowserManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return

        self._browser: Browser | None = None
        self._contexts: dict[str, BrowserContext] = {}
        self._page_pool: list[PagePoolEntry] = []
        self._playwright = None
        self._semaphore = asyncio.Semaphore(settings.browser_concurrency)
        self._pages_created = 0
        self._restart_threshold = settings.browser_restart_interval
        self._initialized = True
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the browser."""
        async with self._lock:
            if "default" in self._contexts:
                return

            try:
                self._playwright = await async_playwright().start()
                
                # Check for system Chrome path
                import shutil
                import os
                chrome_path = shutil.which("google-chrome") or shutil.which("chromium-browser") or shutil.which("chromium")
                
                # Ensure the user data profile directory exists
                user_data_dir = os.getenv("PLAYWRIGHT_USER_DATA_DIR", "./user_profile")
                os.makedirs(user_data_dir, exist_ok=True)
                
                launch_args = {
                    "user_data_dir": user_data_dir,
                    "headless": settings.browser_headless,
                    "user_agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "viewport": {"width": 1920, "height": 1080},
                    "args": [
                        "--disable-gpu",
                        "--disable-dev-shm-usage",
                        "--no-sandbox",
                        "--disable-web-security",
                        "--disable-features=IsolateOrigins,site-per-process",
                        "--disable-blink-features=AutomationControlled",
                    ],
                }
                
                if chrome_path:
                    logger.info("using_system_chrome", path=chrome_path)
                    launch_args["executable_path"] = chrome_path
                elif settings.browser_channel:
                    launch_args["channel"] = settings.browser_channel
                    
                # Launch Chromium as a persistent browser context to share Google Account logins & cookies
                logger.info("launching_persistent_browser_context", user_data_dir=user_data_dir)
                context = await self._playwright.chromium.launch_persistent_context(**launch_args)
                
                # Add default human-like request headers and anti-detection settings
                await context.set_extra_http_headers({
                    "Accept-Language": "en-US,en;q=0.9",
                    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"Windows"',
                })
                
                # Block heavy resources on the persistent context
                async def block_resources(route: Route) -> None:
                    # Do NOT block stylesheets, as Google Maps scrolling container relies on CSS layouts to scroll and load more results.
                    if route.request.resource_type in {
                        "image",
                        "font",
                        "media",
                    }:
                        await route.abort()
                    else:
                        await route.continue_()

                await context.route("**/*", block_resources)
                
                self._contexts["default"] = context
                
                logger.info(
                    "browser_started",
                    headless=settings.browser_headless,
                    using_system_chrome=chrome_path is not None,
                    persistent=True,
                )

            except Exception as e:
                logger.error("browser_start_failed", error=str(e))
                raise

    async def stop(self) -> None:
        """Stop the browser."""
        async with self._lock:
            # Close all pages
            for entry in self._page_pool:
                try:
                    await asyncio.wait_for(entry.page.close(), timeout=1.0)
                except Exception:
                    pass
            self._page_pool.clear()

            # Close all contexts
            for context in list(self._contexts.values()):
                try:
                    await asyncio.wait_for(context.close(), timeout=1.0)
                except Exception:
                    pass
            self._contexts.clear()

            # Close browser
            if self._browser:
                try:
                    await asyncio.wait_for(self._browser.close(), timeout=2.0)
                except Exception:
                    pass
                self._browser = None

            # Stop playwright
            if self._playwright:
                try:
                    await asyncio.wait_for(self._playwright.stop(), timeout=2.0)
                except Exception:
                    pass
                self._playwright = None

            logger.info("browser_stopped")

    async def restart(self) -> None:
        """Restart the browser."""
        logger.info("browser_restarting")
        await self.stop()
        await asyncio.sleep(1)
        await self.start()
        self._pages_created = 0

    async def _create_context(self, context_id: str) -> BrowserContext:
        """Create a new browser context."""
        # With launch_persistent_context, self._browser is None but the default context exists
        if not self._browser and "default" not in self._contexts:
            raise RuntimeError("Browser not started")
        
        # If we used launch_persistent_context, reuse that context for new pages
        if not self._browser and "default" in self._contexts:
            return self._contexts["default"]

        context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            accept_downloads=False,
        )

        # Block heavy resources
        async def block_resources(route: Route) -> None:
            # Do NOT block stylesheets, as Google Maps scrolling container relies on CSS layouts to scroll and load more results.
            if route.request.resource_type in {
                "image",
                "font",
                "media",
            }:
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", block_resources)

        return context

    async def get_context(self, context_id: str = "default") -> BrowserContext:
        """Get or create a browser context."""
        if context_id not in self._contexts:
            self._contexts[context_id] = await self._create_context(context_id)

        return self._contexts[context_id]

    async def _create_new_page_safely(self, context_id: str = "default") -> Page:
        """Create a new page, restarting the browser if the context has been closed."""
        try:
            context = await self.get_context(context_id)
            return await context.new_page()
        except Exception as e:
            if any(x in str(e).lower() for x in ("closed", "connection", "not started", "destroyed")):
                logger.info("browser_context_closed_restarting", error=str(e))
                await self.restart()
                context = await self.get_context(context_id)
                return await context.new_page()
            else:
                raise

    async def _get_page(self, context_id: str = "default") -> Page:
        """Get a page from pool or create new."""
        async with self._lock:
            # Check for available page in pool
            for entry in self._page_pool:
                if not entry.in_use and entry.context_id == context_id:
                    entry.in_use = True
                    return entry.page

        # Create new page safely
        page = await self._create_new_page_safely(context_id)

        async with self._lock:
            self._pages_created += 1

            # Check restart threshold
            if self._pages_created >= self._restart_threshold:
                asyncio.create_task(self.restart())

        return page

    async def _release_page(self, page: Page, context_id: str = "default") -> None:
        """Release page back to pool or close it."""
        try:
            # Clear cookies and local storage
            await page.evaluate("() => { localStorage.clear(); }")

            # Mark as available in pool
            for entry in self._page_pool:
                if entry.page == page:
                    entry.in_use = False
                    return

            # Not in pool, add it
            self._page_pool.append(PagePoolEntry(
                page=page,
                context_id=context_id,
                in_use=False,
            ))

            # Limit pool size
            while len(self._page_pool) > 10:
                old_entry = self._page_pool.pop(0)
                if old_entry.page != page:
                    try:
                        await old_entry.page.close()
                    except Exception:
                        pass

        except Exception as e:
            logger.warning("page_release_error", error=str(e))
            try:
                await page.close()
            except Exception:
                pass

    @asynccontextmanager
    async def page(
        self,
        context_id: str = "default",
    ) -> AsyncGenerator[Page, None]:
        """Get a page from the pool."""
        async with self._semaphore:
            page = await self._get_page(context_id)
            try:
                yield page
            finally:
                await self._release_page(page, context_id)

    # Country code mapping for Google Maps geo-targeting
    _COUNTRY_GEO = {
        "united states": {"gl": "us", "hl": "en", "lat": 39.8283, "lng": -98.5795},
        "usa": {"gl": "us", "hl": "en", "lat": 39.8283, "lng": -98.5795},
        "united kingdom": {"gl": "uk", "hl": "en", "lat": 51.5074, "lng": -0.1278},
        "uk": {"gl": "uk", "hl": "en", "lat": 51.5074, "lng": -0.1278},
        "canada": {"gl": "ca", "hl": "en", "lat": 56.1304, "lng": -106.3468},
        "australia": {"gl": "au", "hl": "en", "lat": -25.2744, "lng": 133.7751},
        "germany": {"gl": "de", "hl": "de", "lat": 51.1657, "lng": 10.4515},
        "singapore": {"gl": "sg", "hl": "en", "lat": 1.3521, "lng": 103.8198},
        "uae": {"gl": "ae", "hl": "en", "lat": 25.2048, "lng": 55.2708},
        "dubai": {"gl": "ae", "hl": "en", "lat": 25.2048, "lng": 55.2708},
        "india": {"gl": "in", "hl": "en", "lat": 20.5937, "lng": 78.9629},
    }

    def _detect_geo_from_area(self, area: str) -> dict[str, Any] | None:
        """Detect geo-targeting params from the search area string."""
        area_lower = area.lower().strip()
        for key, geo in self._COUNTRY_GEO.items():
            if key in area_lower:
                return geo
        return None

    async def goto_maps_search(
        self,
        keyword: str,
        page: Page,
        timeout: int = 120000,
        area: str = "",
    ) -> bool:
        """Navigate to Google Maps search with geo-targeting."""
        try:
            # Build URL with geo-targeting params if area contains a country
            geo = self._detect_geo_from_area(area) if area else None
            
            params = ""
            if geo:
                params = f"?gl={geo['gl']}&hl={geo['hl']}"
                # Spoof geolocation so Google Maps centers on that country
                await page.context.set_geolocation({
                    "latitude": geo["lat"],
                    "longitude": geo["lng"],
                })
                await page.context.grant_permissions(["geolocation"])

            url = f"https://www.google.com/maps/search/{quote(keyword)}{params}"
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)

            # Wait for results feed or direct place page redirect
            try:
                await page.wait_for_selector('div[role="feed"], h1', timeout=15000)
                await asyncio.sleep(3)  # Wait for initial batch of results to fully load
                return True
            except Exception:
                return False

        except Exception as e:
            logger.warning("maps_search_failed", keyword=keyword, error=str(e))
            return False

    async def scroll_maps_results(
        self,
        page: Page,
        max_scrolls: int = 100,
        target_count: int | None = None,
    ) -> list[str]:
        """Scroll through Google Maps results and collect place URLs."""
        # Always allow up to 300 scrolls to make sure we reach the end of the results list
        max_scrolls = max(max_scrolls, 300)

        urls = []
        seen_urls = set()
        no_new_count = 0

        try:
            # Check if we redirected to a single place page directly
            current_url = page.url
            if "/place/" in current_url:
                logger.info("maps_single_place_redirect", url=current_url)
                return [current_url]

            # Wait for results to load
            await page.wait_for_selector('div[role="feed"]', timeout=10000)
            await asyncio.sleep(2)  # Let initial results load

            panel = page.locator('div[role="feed"]').first
            if not panel:
                logger.warning("maps_no_feed_panel")
                return urls

            for scroll_num in range(max_scrolls):
                # Get current links
                current_urls = await page.evaluate(
                    """
                    () => {
                        const links = Array.from(
                            document.querySelectorAll('a[href*="/place/"]')
                        );
                        return links.map(x => x.href).filter(Boolean);
                    }
                    """
                )

                # Add new unique URLs
                new_count = 0
                for url in current_urls:
                    clean_url = url.split("?")[0]
                    if clean_url not in seen_urls:
                        seen_urls.add(clean_url)
                        urls.append(clean_url)
                        new_count += 1

                if new_count > 0:
                    logger.info(
                        "maps_scroll_progress",
                        scroll=scroll_num,
                        new_urls=new_count,
                        total=len(urls)
                    )
                    no_new_count = 0
                else:
                    no_new_count += 1

                # No longer stopping early on target_count; scroll to the very end of Google Maps results list.
                pass

                # Check for end of list text
                try:
                    end_text = await page.locator(
                        'text="You\'ve reached the end of the list"'
                    ).count()
                    if end_text > 0:
                        logger.info("maps_end_reached", total=len(urls))
                        break
                except Exception:
                    pass

                # Stop if no new results for 8 consecutive scrolls
                if no_new_count >= 8:
                    logger.info("maps_no_new_results", total=len(urls))
                    break

                # Scroll the last found place link into view to trigger lazy loading
                try:
                    last_link = page.locator('a[href*="/place/"]').last
                    if await last_link.count() > 0:
                        await last_link.scroll_into_view_if_needed()
                        await asyncio.sleep(0.5)
                except Exception as se:
                    logger.debug("scroll_link_into_view_failed", error=str(se))

                # Scroll to the bottom of the feed panel and its parent container as fallback
                try:
                    await panel.evaluate(
                        """(el) => {
                            el.scrollTo(0, el.scrollHeight);
                            if (el.parentElement) {
                                el.parentElement.scrollTo(0, el.parentElement.scrollHeight);
                            }
                        }"""
                    )
                except Exception:
                    pass
                
                # Wait for potential new results to load
                await asyncio.sleep(2.0)
                
                # Check for loading spinner and wait
                try:
                    loading = await page.locator('[role="progressbar"], .loading, .spinner').count()
                    if loading > 0:
                        await asyncio.sleep(2)
                except Exception:
                    pass

            logger.info("maps_scroll_complete", total_urls=len(urls), scrolls=scroll_num + 1)

        except Exception as e:
            logger.warning("maps_scroll_error", error=str(e))

        return urls

    async def extract_business_details(
        self,
        page: Page,
        url: str,
    ) -> dict[str, Any] | None:
        """Extract business details from Google Maps place page."""
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(1.5)

            # Wait for h1
            try:
                await page.wait_for_selector("h1", timeout=10000)
            except Exception:
                return None

            # Extract details
            details = await page.evaluate(
                """
                () => {
                    const result = { name: "", website: "", phone: "", rating: null, review_count: null, address: "" };
                    const h1 = document.querySelector("h1");
                    if (h1) {
                        result.name = h1.textContent.trim();
                    }

                    // Extract address
                    const addressEl = document.querySelector('[data-item-id="address"]');
                    if (addressEl) {
                        result.address = addressEl.textContent.trim();
                    }

                    // Extract phone
                    // 1. Try to find the Google Maps phone button/link directly
                    const gMapsPhoneEl = document.querySelector('[data-item-id^="phone:tel:"]');
                    if (gMapsPhoneEl) {
                        const itemId = gMapsPhoneEl.getAttribute("data-item-id");
                        result.phone = itemId.replace("phone:tel:", "").trim();
                    } else {
                        // 2. Try tel: links
                        const telLink = document.querySelector('a[href^="tel:"]');
                        if (telLink) {
                            result.phone = telLink.getAttribute("href").replace("tel:", "").trim();
                        } else {
                            // 3. Fallback: Scan text for a phone number match
                            const allElements = document.querySelectorAll("*");
                            let phoneText = "";
                            for (const el of allElements) {
                                phoneText += " " + (
                                    el.innerText ||
                                    el.textContent ||
                                    el.getAttribute("aria-label") ||
                                    el.getAttribute("data-value") ||
                                    ""
                                );
                            }
                            // Generic regex matching international, national, and local formats (7 to 15 digits)
                            const phoneMatch = phoneText.match(
                                /(?:\\+\\d{1,4}[-.\\s]?)?\\(?\\d{2,6}\\)?[-.\\s]?\\d{3,9}[-.\\s]?\\d{3,9}/
                            );
                            if (phoneMatch) {
                                result.phone = phoneMatch[0].trim();
                            }
                        }
                    }

                    // Extract website
                    const links = Array.from(document.querySelectorAll("a"));
                    for (const link of links) {
                        const href = link.href || "";
                        if (
                            href.startsWith("http") &&
                            !href.includes("google.") &&
                            !href.includes("wa.me") &&
                            !href.includes("whatsapp") &&
                            !href.includes("facebook.com") &&
                            !href.includes("instagram.com") &&
                            !href.includes("twitter.com") &&
                            !href.includes("linkedin.com") &&
                            !href.includes("youtube.com") &&
                            !href.includes("api.whatsapp.com")
                        ) {
                            result.website = href;
                            break;
                        }
                    }

                    // Helper to parse review count with optional K/M suffixes
                    const parseReviewCount = (rawStr) => {
                        if (!rawStr) return null;
                        let cleanStr = rawStr.toLowerCase().replace(/[(),\\s]/g, "").replace(/,/g, "");
                        if (cleanStr.includes("k")) {
                            return Math.round(parseFloat(cleanStr.replace("k", "")) * 1000);
                        }
                        if (cleanStr.includes("m")) {
                            return Math.round(parseFloat(cleanStr.replace("m", "")) * 1000000);
                        }
                        const num = parseInt(cleanStr, 10);
                        return isNaN(num) ? null : num;
                    };

                    // Extract rating & review count
                    try {
                        const ratingContainer = document.querySelector(".F7nice");
                        if (ratingContainer) {
                            const text = ratingContainer.textContent.trim();
                            
                            // Extract rating (usually the first number in the text, e.g. "4.9")
                            const ratingMatch = text.match(/^([0-9.]+)/);
                            if (ratingMatch) {
                                const parsedRating = parseFloat(ratingMatch[1]);
                                if (!isNaN(parsedRating)) {
                                    result.rating = parsedRating;
                                }
                            }
                            
                            // Extract review count (usually the text inside parentheses, e.g. "(1,234)" or "(4)")
                            const reviewsMatch = text.match(/\\(([^)]+)\\)/);
                            if (reviewsMatch) {
                                const parsedReviews = parseReviewCount(reviewsMatch[1]);
                                if (parsedReviews !== null) {
                                    result.review_count = parsedReviews;
                                }
                            }
                        }
                        
                        // Fallback 1: aria-label with stars/reviews
                        if (result.rating === null) {
                            const starsEl = document.querySelector('span[aria-label*="stars"], span[aria-label*="star"]');
                            if (starsEl) {
                                const label = starsEl.getAttribute("aria-label");
                                const match = label.match(/([0-9.]+)\\s*star/i);
                                if (match) {
                                    const parsedRating = parseFloat(match[1]);
                                    if (!isNaN(parsedRating)) {
                                        result.rating = parsedRating;
                                    }
                                }
                            }
                        }
                        if (result.review_count === null) {
                            const reviewsEl = document.querySelector('button[aria-label*="reviews"], span[aria-label*="reviews"]');
                            if (reviewsEl) {
                                const label = reviewsEl.getAttribute("aria-label");
                                const match = label.match(/([0-9,kKmM.]+)\\s*reviews/i);
                                if (match) {
                                    const parsedReviews = parseReviewCount(match[1]);
                                    if (parsedReviews !== null) {
                                        result.review_count = parsedReviews;
                                    }
                                }
                            }
                        }
                        
                        // Fallback 2: obfuscated classes
                        if (result.rating === null) {
                            const mw4 = document.querySelector('.MW4etd');
                            if (mw4) {
                                const parsedRating = parseFloat(mw4.textContent.trim());
                                if (!isNaN(parsedRating)) {
                                    result.rating = parsedRating;
                                }
                            }
                        }
                        if (result.review_count === null) {
                            const rnb = document.querySelector('.RNBzgc');
                            if (rnb) {
                                const parsedReviews = parseReviewCount(rnb.textContent);
                                if (parsedReviews !== null) {
                                    result.review_count = parsedReviews;
                                }
                            }
                        }
                    } catch (err) {
                        // ignore errors
                    }

                    return result;
                }
                """
            )

            if not details.get("name"):
                return None

            # Clean website URL
            website = details.get("website", "")
            if website:
                from urllib.parse import urlparse
                parsed = urlparse(website)
                netloc = parsed.netloc.lower()

                # Skip URL shorteners
                blocked = ["wa.link", "calendly.com", "bit.ly", "tinyurl", "short.link"]
                if any(x in netloc for x in blocked):
                    website = ""
                else:
                    website = f"{parsed.scheme}://{parsed.netloc}/"

            return {
                "name": details["name"],
                "website": website,
                "phone": details.get("phone", ""),
                "rating": details.get("rating"),
                "review_count": details.get("review_count"),
                "address": details.get("address", ""),
            }

        except Exception as e:
            logger.warning("business_details_extraction_failed", url=url, error=str(e))
            return None

    @asynccontextmanager
    async def website_page(
        self,
        website: str,
    ) -> AsyncGenerator[Page, None]:
        """Get a page for website scraping.
        
        Reuses the persistent default context (created by launch_persistent_context)
        for all website scraping tasks, since isolated contexts require self._browser
        which is not available when using the persistent context approach.
        """
        async with self._semaphore:
            page = await self._create_new_page_safely("default")
            try:
                yield page
            finally:
                try:
                    await page.close()
                except Exception:
                    pass


# Global instance getter
def get_browser_manager() -> BrowserManager:
    """Get the BrowserManager singleton instance."""
    return BrowserManager()
