"""Enrichment service - Website contact extraction.

Simplified v2 — no Redis, no cache, no WebSocket.
Scrapes websites directly to extract emails, phones, social links.
"""

import asyncio
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from sqlalchemy.ext.asyncio import AsyncSession

from core.browser_manager import get_browser_manager
from core.config import settings
from core.logging import get_logger
from services.enrichment.phone import verify_phone
from services.enrichment.linkedin import verify_linkedin_profile

logger = get_logger(__name__)


# Contact extraction patterns
EMAIL_PATTERN = re.compile(
    r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b",
    re.IGNORECASE
)

PHONE_PATTERN = re.compile(
    r"(?:\+91[-\s]?)?(?:\d{2,5}[-\s]?\d{5,8})|(?:\+91[-\s]?)?\d{10}",
    re.IGNORECASE
)

SOCIAL_PATTERNS = {
    "linkedin": re.compile(r"linkedin\.com/(?:company|in)/[^\s\"<>]+", re.I),
    "facebook": re.compile(r"facebook\.com/[^\s\"<>]+", re.I),
    "instagram": re.compile(r"instagram\.com/[^\s\"<>]+", re.I),
    "twitter": re.compile(r"twitter\.com/[^\s\"<>]+|x\.com/[^\s\"<>]+", re.I),
    "youtube": re.compile(r"youtube\.com/(?:channel|c|user)/[^\s\"<>]+|youtu\.be/[^\s\"<>]+", re.I),
}

HIGH_VALUE_PAGES = [
    "contact", "contact-us", "contactus", "about", "about-us", "aboutus",
    "team", "our-team", "careers", "jobs", "support",
    "locations", "branches", "offices", "get-in-touch", "reach-us"
]


import phonenumbers

def clean_phone(phone: str, default_region: str = "IN") -> str:
    """Clean and standardize phone number using Google's phonenumbers library.
    
    Returns:
    - 10-digit number for Indian numbers (no +91 prefix) as expected by the DB/UI.
    - E.164 format (e.g. +971543470278) for international numbers.
    """
    if not phone:
        return ""
    try:
        # Normalize/clean spaces and brackets first so phonenumbers can parse it cleanly
        cleaned_input = phone.strip()
        # Parse using the detected default region
        parsed = phonenumbers.parse(cleaned_input, default_region)
        if phonenumbers.is_possible_number(parsed):
            if parsed.country_code == 91:
                # Standard Indian 10-digit national number
                return str(parsed.national_number)
            else:
                # E.164 format for international
                return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        pass
    
    # Fallback to simple cleaning if phonenumbers fails
    digits = re.sub(r"\D", "", phone)
    if phone.strip().startswith("+"):
        return f"+{digits}"
    return digits


def is_valid_phone_number(raw_phone: str, cleaned_phone: str, default_region: str = "IN") -> bool:
    """Validate extracted phone number to filter out false positives."""
    if not cleaned_phone:
        return False
        
    try:
        # Parse and check validity
        parsed = phonenumbers.parse(cleaned_phone, default_region)
        if phonenumbers.is_valid_number(parsed):
            check_digits = str(parsed.national_number)
            # Filter out dummy repeating digits (e.g. 9999999999 or any digit repeating 6+ times)
            if re.search(r"(\d)\1{5,}", check_digits):
                return False
            # Filter out common sequential test sequences
            if check_digits in ("1234567890", "0123456789", "9876543210"):
                return False
            return True
    except Exception:
        pass
        
    # Fallback validation for edge cases
    check_digits = cleaned_phone.lstrip("+")
    if not (7 <= len(check_digits) <= 15):
        return False
    if re.search(r"(\d)\1{5,}", check_digits):
        return False
    if check_digits in ("1234567890", "0123456789", "9876543210"):
        return False
    return True



class EnrichmentService:
    """Website enrichment service."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.browser = get_browser_manager()

    async def _verify_email_address(self, email: str) -> bool:
        """Verify email syntax and deliverability (MX records) asynchronously."""
        email = email.strip()
        
        # Blacklist of placeholder emails
        placeholder_blacklist = {
            "email@example.com", "username@domain.com", "yourname@domain.com",
            "email@domain.com", "name@domain.com", "test@test.com", "info@example.com",
            "user@domain.com", "yourname@yourdomain.com", "test@domain.com"
        }
        if email.lower() in placeholder_blacklist:
            return False
            
        def check():
            try:
                from email_validator import validate_email, EmailNotValidError
                validate_email(email, check_deliverability=True, timeout=2.5)
                return True
            except EmailNotValidError:
                return False
            except Exception:
                # If there is a temporary network glitch or query timeout, default to True for safety
                return True
                
        return await asyncio.to_thread(check)

    async def enrich_website(
        self,
        website: str,
        name_hint: str = "",
        default_region: str = "IN",
        search_keyword: str = "",
    ) -> dict[str, Any] | None:
        """Enrich a single website — extract contacts.

        Args:
            website: The website URL to scrape.
            name_hint: Business name hint for fallback.
            default_region: Detected country code.
            search_keyword: The search query used to find the business.

        Returns:
            Dict with emails, phones, social_links, etc. or None if failed.
        """
        logger.info("enrichment_started", website=website)

        # Check database cache first
        if self.db and website:
            try:
                from sqlalchemy import select
                from db.models.lead import Lead
                from urllib.parse import urlparse

                # Normalize to look up both www and non-www variants
                parsed = urlparse(website)
                netloc = parsed.netloc
                alt_netloc = netloc[4:] if netloc.startswith("www.") else f"www.{netloc}"
                alt_website = website.replace(netloc, alt_netloc)

                stmt = select(Lead).where(
                    Lead.website.in_([website, alt_website])
                ).order_by(Lead.created_at.desc()).limit(1)
                
                query_res = await self.db.execute(stmt)
                existing_lead = query_res.scalar_one_or_none()
                
                if existing_lead and (
                    existing_lead.emails or 
                    existing_lead.phones or 
                    existing_lead.whatsapp_numbers or 
                    existing_lead.social_links
                ):
                    # Check if the cached enrichment data contains verification metadata
                    cached_data = existing_lead.enrichment_data or {}
                    has_verification = "verification" in cached_data
                    has_advanced_linkedin = False
                    if has_verification:
                        linkedin_data = cached_data["verification"].get("linkedin", {})
                        # If profile is verified, check if we have scraped advanced fields (like followers or headquarters)
                        if linkedin_data.get("verified"):
                            has_advanced_linkedin = "followers" in linkedin_data or "headquarters" in linkedin_data
                        else:
                            has_advanced_linkedin = True  # Not verified, so no advanced info needed
                    
                        # Filter out any newly classified invalid phones from the cached results
                        cleaned_cached_phones = []
                        for phone in (existing_lead.phones or []):
                            v_res = verify_phone(phone, default_region)
                            if v_res["valid"]:
                                cleaned_cached_phones.append(v_res["cleaned"])
                        
                        # Also filter the verification details list
                        v_details = cached_data["verification"].get("phones", [])
                        filtered_v_details = []
                        for item in v_details:
                            v_res = verify_phone(item.get("cleaned", ""), default_region)
                            if v_res["valid"]:
                                filtered_v_details.append(v_res)
                        cached_data["verification"]["phones"] = filtered_v_details

                        logger.info("enrichment_cache_hit", website=website, lead_id=str(existing_lead.id))
                        print(f"Cache Hit: {website} (Reusing contacts)")
                        return {
                            "company": existing_lead.company_name or name_hint,
                            "website": existing_lead.website or website,
                            "emails": existing_lead.emails,
                            "phones": cleaned_cached_phones,
                            "whatsapp_numbers": existing_lead.whatsapp_numbers,
                            "addresses": [existing_lead.address] if existing_lead.address else [],
                            "social_links": existing_lead.social_links,
                            "contact_pages": cached_data.get("contact_pages", []),
                            "pages_crawled": existing_lead.pages_crawled,
                            "verification": cached_data["verification"],
                        }
                    else:
                        logger.info("enrichment_cache_ignored_no_verification_or_advanced_details", website=website)
                        print(f"Cache Ignored (No advanced verification data): {website}")
            except Exception as e:
                logger.warning("enrichment_cache_lookup_failed", website=website, error=str(e))

        print(f"Scraping: {website}")
        result = await self._scrape_website(website, name_hint, default_region, search_keyword)

        if result:
            raw_emails = result.get("emails", [])
            verified_emails = []
            for email in raw_emails:
                if await self._verify_email_address(email):
                    verified_emails.append(email)
            result["emails"] = verified_emails

            emails = result.get("emails", [])
            phones = result.get("phones", [])
            print(f"Success: {name_hint or website}: {len(emails)} emails (verified), {len(phones)} phones")

            # --- Verification Phase ---
            # 1. Verify Phone Numbers
            verified_phones_details = []
            valid_phones = []
            for phone in phones:
                v_res = verify_phone(phone, default_region)
                verified_phones_details.append(v_res)
                if v_res["valid"]:
                    valid_phones.append(v_res["cleaned"])
            
            # Always verify and include the main listing phone if available
            if result.get("phone"):
                v_res = verify_phone(result["phone"], default_region)
                # Avoid duplicates in verified details
                if not any(d.get("cleaned") == v_res["cleaned"] for d in verified_phones_details):
                    verified_phones_details.append(v_res)
                if v_res["valid"] and v_res["cleaned"] not in valid_phones:
                    valid_phones.append(v_res["cleaned"])

            result["phones"] = list(set(valid_phones))

            # 2. LinkedIn Profile Verification (via DuckDuckGo)
            linkedin_details = {
                "verified": False,
                "linkedin_url": None,
                "name": None,
                "role": None,
                "company": None,
                "matched_method": "NONE",
            }
            # Try searching by email
            for email in verified_emails:
                v_res = await verify_linkedin_profile(email=email, company_name=name_hint, verified_emails=verified_emails)
                if v_res["verified"]:
                    linkedin_details = v_res
                    break
            # Fallback to company name search if no email-based match
            if not linkedin_details["verified"] and name_hint:
                v_res = await verify_linkedin_profile(email=None, company_name=name_hint, verified_emails=verified_emails)
                if v_res["verified"]:
                    linkedin_details = v_res

            # Add verified LinkedIn URL to social links if found
            if linkedin_details["verified"] and linkedin_details["linkedin_url"]:
                social_links = result.get("social_links", [])
                if linkedin_details["linkedin_url"] not in social_links:
                    social_links.append(linkedin_details["linkedin_url"])
                result["social_links"] = social_links

            # Save verification metadata
            if result.get("founded_year") and not linkedin_details.get("founded"):
                linkedin_details["founded"] = result["founded_year"]

            result["verification"] = {
                "phones": verified_phones_details,
                "linkedin": linkedin_details,
            }
        else:
            print(f"Failed: {name_hint or website}: no contacts found")

        return result

    async def _scrape_website_httpx(
        self,
        website: str,
        name_hint: str,
        default_region: str = "IN",
        search_keyword: str = "",
    ) -> dict[str, Any] | None:
        """Attempt to scrape the website using fast HTTP requests first."""
        logger.info("httpx_enrichment_attempt", website=website)
        company = ""
        emails: set[str] = set()
        phones: set[str] = set()
        whatsapp_numbers: set[str] = set()
        addresses: list[str] = []
        social_links: set[str] = set()
        contact_pages: list[str] = []
        pages_crawled = 0

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

        import httpx
        timeout = httpx.Timeout(15.0, connect=10.0)
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=timeout) as client:
            try:
                resp = await client.get(website)
                if resp.status_code != 200:
                    logger.info("httpx_homepage_non_200", website=website, status=resp.status_code)
                    return None

                # Verify Content-Type is HTML
                content_type = resp.headers.get("Content-Type", "").lower()
                if "text/html" not in content_type:
                    logger.info("httpx_homepage_not_html", website=website, content_type=content_type)
                    return None

                html = resp.text
                
                title_match = re.search(r"<title>(.*?)</title>", html, re.I)
                if title_match:
                    company = title_match.group(1).split("|")[0].split("-")[0].strip()
                else:
                    company = name_hint

                text = re.sub(r'<[^>]+>', ' ', html)
                contacts = self._extract_contacts(text, html, default_region)
                founded_year = self._extract_founded_year(text)
                
                emails.update(contacts["emails"])
                phones.update(contacts["phones"])
                whatsapp_numbers.update(contacts["whatsapp"])
                addresses.extend(contacts["addresses"])
                social_links.update(contacts["social_links"])
                pages_crawled = 1

                # Get homepage links & sitemap links
                links = self._find_contact_links(html, website, search_keyword)
                sitemap_links = await self._get_sitemap_links(website)

                # Merge links, removing duplicates
                all_links_set = set(links)
                for link in sitemap_links:
                    all_links_set.add(link)

                # Recalculate priority scores for the merged set
                scored_links = []
                for link in all_links_set:
                    priority = self._get_page_priority(link, search_keyword)
                    scored_links.append((priority, link))

                # Sort by priority
                scored_links.sort(key=lambda x: -x[0])
                sorted_links = [url for _, url in scored_links]

                max_pages = settings.max_pages_per_site

                visited = {website}
                for url in sorted_links:
                    if pages_crawled >= max_pages:
                        break
                    if url in visited:
                        continue

                    try:
                        p_resp = await client.get(url)
                        if p_resp.status_code == 200:
                            # Verify Content-Type is HTML
                            content_type = p_resp.headers.get("Content-Type", "").lower()
                            if "text/html" not in content_type:
                                continue
                            p_html = p_resp.text
                            p_text = re.sub(r'<[^>]+>', ' ', p_html)
                            p_contacts = self._extract_contacts(p_text, p_html, default_region)

                            if not founded_year:
                                founded_year = self._extract_founded_year(p_text)

                            if p_contacts["emails"] or p_contacts["phones"] or p_contacts["social_links"]:
                                contact_pages.append(url)
                                emails.update(p_contacts["emails"])
                                phones.update(p_contacts["phones"])
                                whatsapp_numbers.update(p_contacts["whatsapp"])
                                addresses.extend(p_contacts["addresses"])
                                social_links.update(p_contacts["social_links"])

                            visited.add(url)
                            pages_crawled += 1
                    except Exception as e:
                        logger.debug("httpx_page_crawl_failed", url=url, error=str(e))
                        continue

            except Exception as e:
                logger.info("httpx_homepage_failed", website=website, error=str(e))
                raise

        if not emails and not phones and not whatsapp_numbers and not social_links:
            logger.info("httpx_no_contacts_found", website=website)
            return None

        return {
            "company": company or name_hint,
            "website": str(resp.url),
            "emails": list(emails),
            "phones": list(phones),
            "whatsapp_numbers": list(whatsapp_numbers),
            "addresses": list(set(addresses))[:5],
            "social_links": list(social_links),
            "contact_pages": contact_pages,
            "pages_crawled": pages_crawled,
            "founded_year": founded_year,
        }

    async def _scrape_website(
        self,
        website: str,
        name_hint: str,
        default_region: str = "IN",
        search_keyword: str = "",
    ) -> dict[str, Any] | None:
        """Scrape website for contact information."""
        # Try HTTPX first
        try:
            parsed = urlparse(website)
            urls_to_try = [website]
            if parsed.netloc.startswith("www."):
                alt_netloc = parsed.netloc[4:]
                alt_url = parsed._replace(netloc=alt_netloc).geturl()
                urls_to_try.append(alt_url)
            else:
                alt_netloc = "www." + parsed.netloc
                alt_url = parsed._replace(netloc=alt_netloc).geturl()
                urls_to_try.append(alt_url)

            for url in urls_to_try:
                try:
                    result = await self._scrape_website_httpx(url, name_hint, default_region, search_keyword)
                    if result:
                        has_emails = len(result.get("emails", [])) > 0
                        has_valid_phones = any(verify_phone(p, default_region)["valid"] for p in result.get("phones", []))
                        if has_emails or has_valid_phones:
                            logger.info("enrichment_httpx_success", website=url)
                            return result
                        else:
                            logger.info("enrichment_httpx_empty_contacts_falling_back", website=url)
                except Exception as e:
                    logger.debug("enrichment_httpx_attempt_failed", url=url, error=str(e))
                    continue
        except Exception as e:
            logger.warning("enrichment_httpx_error", website=website, error=str(e))

        # Fallback to Playwright
        logger.info("enrichment_playwright_fallback", website=website)
        company = ""
        emails: set[str] = set()
        phones: set[str] = set()
        whatsapp_numbers: set[str] = set()
        addresses: list[str] = []
        social_links: set[str] = set()
        contact_pages: list[str] = []
        pages_crawled = 0
        founded_year = None

        final_website_url = website
        parsed = urlparse(website)
        urls_to_try = [website]
        if parsed.netloc.startswith("www."):
            alt_netloc = parsed.netloc[4:]
            alt_url = parsed._replace(netloc=alt_netloc).geturl()
            urls_to_try.append(alt_url)
        else:
            alt_netloc = "www." + parsed.netloc
            alt_url = parsed._replace(netloc=alt_netloc).geturl()
            urls_to_try.append(alt_url)

        playwright_success = False
        for url in urls_to_try:
            logger.info("enrichment_playwright_attempt", url=url)
            try:
                async with self.browser.website_page(url) as page:
                    # Visit homepage
                    await page.goto(url, timeout=15000, wait_until="domcontentloaded")
                    await asyncio.sleep(2)
                    final_website_url = page.url

                    # Get title
                    try:
                        title = await page.title()
                        company = title.split("|")[0].split("-")[0].strip()
                    except Exception:
                        company = name_hint

                    # Extract from homepage
                    html = await page.content()
                    text = await page.evaluate("() => document.body?.innerText || ''")

                    contacts = self._extract_contacts(text, html, default_region)
                    founded_year = self._extract_founded_year(text)

                    emails.update(contacts["emails"])
                    phones.update(contacts["phones"])
                    whatsapp_numbers.update(contacts["whatsapp"])
                    addresses.extend(contacts["addresses"])
                    social_links.update(contacts["social_links"])
                    pages_crawled = 1

                    # Get homepage links & sitemap links
                    links = self._find_contact_links(html, url, search_keyword)
                    sitemap_links = await self._get_sitemap_links(url)

                    # Merge links, removing duplicates
                    all_links_set = set(links)
                    for link in sitemap_links:
                        all_links_set.add(link)

                    # Recalculate priority scores for the merged set
                    scored_links = []
                    for link in all_links_set:
                        priority = self._get_page_priority(link, search_keyword)
                        scored_links.append((priority, link))

                    # Sort by priority
                    scored_links.sort(key=lambda x: -x[0])
                    sorted_links = [p_url for _, p_url in scored_links]

                    # Crawl high-value pages
                    visited = {url}
                    max_pages = settings.max_pages_per_site

                    for page_url in sorted_links:
                        if pages_crawled >= max_pages:
                            break
                        if page_url in visited:
                            continue

                        try:
                            await page.goto(page_url, timeout=15000, wait_until="domcontentloaded")
                            await asyncio.sleep(2)

                            html = await page.content()
                            text = await page.evaluate("() => document.body?.innerText || ''")

                            page_contacts = self._extract_contacts(text, html, default_region)

                            if not founded_year:
                                founded_year = self._extract_founded_year(text)

                            if page_contacts["emails"] or page_contacts["phones"] or page_contacts["social_links"]:
                                contact_pages.append(page_url)
                                emails.update(page_contacts["emails"])
                                phones.update(page_contacts["phones"])
                                whatsapp_numbers.update(page_contacts["whatsapp"])
                                addresses.extend(page_contacts["addresses"])
                                social_links.update(page_contacts["social_links"])

                            visited.add(page_url)
                            pages_crawled += 1

                        except Exception:
                            continue

                    playwright_success = True
                    break

            except Exception as e:
                logger.warning("playwright_attempt_failed", url=url, error=str(e))
                continue

        if not playwright_success:
            logger.warning("website_scrape_failed", website=website)
            return None

        if not emails and not phones and not whatsapp_numbers and not social_links:
            return None

        return {
            "company": company,
            "website": final_website_url,
            "emails": list(emails),
            "phones": list(phones),
            "whatsapp_numbers": list(whatsapp_numbers),
            "addresses": list(set(addresses))[:5],
            "social_links": list(social_links),
            "contact_pages": contact_pages,
            "pages_crawled": pages_crawled,
            "founded_year": founded_year,
        }

    def _extract_founded_year(self, text: str) -> str | None:
        """Extract founded/established year from text."""
        if not text:
            return None
        # Look for "established in 2011", "founded in 2011", "since 2011", "est. 2011"
        match = re.search(r'\b(?:established|founded|started|since|incorporated|est\.)\b.{1,20}\b(18\d\d|19\d\d|20[0-2]\d)\b', text, re.I)
        if match:
            return match.group(1)
        # Fallback to year near keywords
        match = re.search(r'\b(18\d\d|19\d\d|20[0-2]\d)\b.{1,20}\b(?:established|founded|est\.)\b', text, re.I)
        if match:
            return match.group(1)
        return None

    def _extract_contacts(self, text: str, html: str, default_region: str = "IN") -> dict[str, Any]:
        """Extract contact information from text and HTML using phonenumbers."""
        results = {
            "emails": set(),
            "phones": set(),
            "whatsapp": set(),
            "social_links": set(),
            "addresses": [],
        }

        # Extract emails
        raw_emails = set(EMAIL_PATTERN.findall(text))

        # Extract mailto links
        mailto_matches = re.findall(r'mailto:([^"\s<>]+)', html, re.I)
        raw_emails.update(mailto_matches)

        ignored_email_extensions = (
            ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", 
            ".js", ".css", ".pdf", ".zip", ".tar", ".gz", ".ico",
            ".mp4", ".mov", ".avi", ".mp3", ".wav"
        )
        for email in raw_emails:
            email_lower = email.lower().strip()
            if not email_lower.endswith(ignored_email_extensions):
                results["emails"].add(email)

        # Extract tel links (href="tel:..." or general tel: prefix)
        tel_matches = re.findall(r'href=["\']tel:([^"\']+)["\']', html, re.I)
        # Fallback to general tel: matching
        tel_matches.extend(re.findall(r'tel:([\d+\-\s()]+)', html, re.I))

        # Extract phone numbers from WhatsApp link hrefs (phone parameters)
        wa_phone_matches = re.findall(r'wa\.me/(\d+)|whatsapp\.com/send\?phone=(\d+)', html, re.I)
        for match in wa_phone_matches:
            num = match[0] if match[0] else match[1]
            if num:
                tel_matches.append(num)

        # Extract phone numbers from anchor tag text contents (e.g., <a ...>+91 8940833985</a>)
        anchor_texts = re.findall(r'<a\b[^>]*>([\s\S]*?)</a>', html, re.I)
        for text_content in anchor_texts:
            # Clean HTML tags inside the anchor text if any (e.g. span, strong)
            clean_text = re.sub(r'<[^>]*>', '', text_content).strip()
            # If the clean text contains 6 to 15 digits, we add it to candidates
            digits_only = re.sub(r'\D', '', clean_text)
            if 6 <= len(digits_only) <= 15:
                tel_matches.append(clean_text)
        
        # Extract phone numbers from body text using PHONE_PATTERN
        text_matches = PHONE_PATTERN.findall(text)
        for match in text_matches:
            if match:
                tel_matches.append(match)

        # Extract phone numbers from body text using Google's phonenumbers library Matcher
        try:
            for match in phonenumbers.PhoneNumberMatcher(text, default_region):
                formatted = phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.E164)
                tel_matches.append(formatted)
        except Exception:
            pass
        
        for tel in set(tel_matches):
            cleaned = clean_phone(tel, default_region)
            if is_valid_phone_number(tel, cleaned, default_region):
                results["phones"].add(cleaned)

        # Extract WhatsApp
        for match in wa_phone_matches:
            num = match[0] if match[0] else match[1]
            if num:
                cleaned = clean_phone(num, default_region)
                results["whatsapp"].add(cleaned)

        # Extract social links
        for platform, pattern in SOCIAL_PATTERNS.items():
            matches = pattern.findall(html)
            for match in matches:
                if match.startswith("http"):
                    results["social_links"].add(match)
                else:
                    results["social_links"].add(f"https://{match}")

        return results

    def _find_contact_links(self, html: str, base_url: str, search_keyword: str = "") -> list[str]:
        """Find contact-related links."""
        links = []
        parsed_base = urlparse(base_url)
        base_domain = parsed_base.netloc
        base_domain_clean = base_domain[4:] if base_domain.startswith("www.") else base_domain
        seen = set()

        href_matches = re.findall(r'href=["\']([^"\']+)["\']', html)

        # Ignore common static asset extensions to prevent unnecessary crawls and regex hangs
        ignored_extensions = (
            '.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.pdf', 
            '.zip', '.tar', '.gz', '.woff', '.woff2', '.ttf', '.eot', '.ico', 
            '.mp4', '.avi', '.mov', '.mp3', '.wav', '.xml', '.json', '.txt',
            '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.map'
        )

        for href in href_matches:
            if href.startswith("#") or href.startswith("javascript:"):
                continue

            # Skip asset URLs
            parsed_href = urlparse(href)
            path = parsed_href.path.lower()
            if path.endswith(ignored_extensions):
                continue

            if href.startswith("http"):
                full_url = href
            elif href.startswith("/"):
                full_url = f"{parsed_base.scheme}://{base_domain}{href}"
            else:
                full_url = urljoin(base_url, href)

            parsed_link = urlparse(full_url)
            link_domain = parsed_link.netloc
            link_domain_clean = link_domain[4:] if link_domain.startswith("www.") else link_domain
            if base_domain_clean != link_domain_clean:
                continue

            # Prioritize pages
            priority = self._get_page_priority(full_url, search_keyword)

            if full_url not in seen:
                seen.add(full_url)
                links.append((priority, full_url))

        # Sort by priority
        links.sort(key=lambda x: -x[0])
        return [url for _, url in links]

    def _get_page_priority(self, url: str, search_keyword: str = "") -> int:
        """Get crawl priority for a URL:
        - 3: High-value contact/about/support pages (Highest priority)
        - 2: Keyword-matching campaign pages (Medium priority)
        - 1: Standard pages (Normal priority)
        """
        url_lower = url.lower()
        
        # Priority 3: Contact, About, Support, enquiry, locations, careers, legal, refund & faq pages
        contact_keywords = [
            "contact", "contact-us", "contactus", "about", "about-us", "aboutus",
            "support", "get-in-touch", "reach-us", "offices", "branches", "locations",
            "enquiry", "enquire", "feedback", "get-quote", "quote", "careers", "jobs",
            "team", "our-team", "ourteam", "find-us", "findus", "help", "helpdesk", "info",
            "privacy", "privacy-policy", "terms", "terms-of-service", "legal",
            "refund", "return", "returns", "cancellation", "faq", "faqs", "help-center",
            "helpcenter", "headquarters", "hq", "work-with-us", "join-us", "recruitment"
        ]
        if any(kw in url_lower for kw in contact_keywords):
            return 3

        if search_keyword:
            # Split search keyword into lowercase words of length 3+
            words = [w.strip().lower() for w in re.split(r'\s+|-|_', search_keyword) if len(w.strip()) >= 3]
            parsed = urlparse(url)
            path_lower = parsed.path.lower()
            if any(word in path_lower for word in words):
                return 2

        return 1

    async def _get_sitemap_links(self, base_url: str) -> list[str]:
        """Fetch and parse sitemap.xml or sitemap_index.xml for URLs."""
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        sitemap_urls = [
            f"{parsed.scheme}://{parsed.netloc}/sitemap.xml",
            f"{parsed.scheme}://{parsed.netloc}/sitemap_index.xml",
        ]

        extracted_links = []
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }

        # Ignore common static asset extensions to prevent unnecessary crawls and regex hangs
        ignored_extensions = (
            '.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.pdf', 
            '.zip', '.tar', '.gz', '.woff', '.woff2', '.ttf', '.eot', '.ico', 
            '.mp4', '.avi', '.mov', '.mp3', '.wav', '.xml', '.json', '.txt',
            '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.map'
        )

        base_domain = parsed.netloc
        base_domain_clean = base_domain[4:] if base_domain.startswith("www.") else base_domain

        import httpx
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=10.0) as client:
            for sitemap_url in sitemap_urls:
                try:
                    resp = await client.get(sitemap_url)
                    if resp.status_code == 200:
                        # Extract all links inside <loc>...</loc> tags
                        links = re.findall(r"<loc>(.*?)</loc>", resp.text, re.I)
                        for link in links:
                            link_clean = link.strip()
                            if link_clean.startswith("http"):
                                parsed_link = urlparse(link_clean)
                                link_domain = parsed_link.netloc
                                link_domain_clean = link_domain[4:] if link_domain.startswith("www.") else link_domain

                                # Filter same domain
                                if base_domain_clean != link_domain_clean:
                                    continue

                                # Filter ignored extensions
                                if parsed_link.path.lower().endswith(ignored_extensions):
                                    continue

                                extracted_links.append(link_clean)
                        # If we found links, we don't need to try the other sitemap URL
                        if extracted_links:
                            break
                except Exception:
                    continue
        return list(set(extracted_links))
