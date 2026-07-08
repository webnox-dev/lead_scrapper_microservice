"""LinkedIn verification service using Yahoo and DuckDuckGo search."""

import re
import urllib.parse
from typing import Any
import httpx

from core.browser_manager import get_browser_manager
from core.logging import get_logger

logger = get_logger(__name__)

# List of generic email prefixes that do not represent an individual's name
GENERIC_PREFIXES = {
    "info", "sales", "support", "contact", "hello", "admin", "jobs", "careers",
    "office", "hr", "marketing", "team", "help", "billing", "inquiry", "enquiry",
    "service", "webmaster", "postmaster", "hostmaster", "noreply", "no-reply",
    "feedback", "staff", "query", "general", "orders", "accounts", "media"
}


def extract_name_from_email(email: str) -> str | None:
    """Extract a person's name from their email prefix if it's not a generic email.
    
    Examples:
    - john.doe@domain.com -> "John Doe"
    - karthik-g@webnox.in -> "Karthik G"
    - sales@domain.com -> None
    """
    if not email or "@" not in email:
        return None
        
    prefix = email.split("@")[0].lower().strip()
    
    # Check if prefix is generic
    if prefix in GENERIC_PREFIXES:
        return None
        
    # Remove numbers or common symbols at the end
    prefix = re.sub(r"\d+$", "", prefix)
    
    # Split by common delimiters (dots, hyphens, underscores)
    parts = re.split(r"[._\-]", prefix)
    
    # Clean parts and filter out empty strings
    parts = [p.capitalize() for p in parts if len(p) > 1]
    
    if not parts:
        return None
        
    return " ".join(parts)


async def search_yahoo_playwright(query: str) -> list[dict[str, str]]:
    """Fetch search results from Yahoo using Playwright (no captcha wall)."""
    url = f"https://search.yahoo.com/search?q={urllib.parse.quote(query)}"
    browser = get_browser_manager()
    
    try:
        async with browser.page() as page:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            
            # Extract parsed results using container-based logic
            results = await page.evaluate(
                """
                () => {
                    const items = [];
                    // Yahoo search results are typically in div.algo or li elements
                    const blocks = Array.from(document.querySelectorAll('.algo, li'));
                    for (const block of blocks) {
                        const link = block.querySelector('a[href*="linkedin.com"]');
                        if (!link) continue;
                        
                        const href = link.href;
                        // Avoid duplicate blocks
                        if (items.some(item => item.url === href)) continue;
                        
                        // Try to find the title
                        const titleEl = block.querySelector('h3, .title, a');
                        const title = titleEl ? titleEl.innerText : '';
                        
                        // Try to find the snippet
                        const snippetEl = block.querySelector('.compText, p, .lh-22, .lh-20');
                        const snippet = snippetEl ? snippetEl.innerText : '';
                        
                        items.push({ url: href, title, snippet });
                    }
                    return items.filter(x => x.url.includes('/in/') || x.url.includes('/company/'));
                }
                """
            )
            return results
    except Exception as e:
        logger.warning("yahoo_search_playwright_failed", error=str(e))
        return []


async def fetch_company_details_playwright(url: str) -> dict[str, Any]:
    """Visit the public LinkedIn company page to scrape advanced details."""
    browser = get_browser_manager()
    try:
        async with browser.page() as page:
            await page.set_extra_http_headers({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9"
            })
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            
            details = await page.evaluate(
                """
                () => {
                    const data = {};
                    
                    // 1. Extract About/Description
                    const aboutHeader = Array.from(document.querySelectorAll('h2, h3, h4')).find(el => el.innerText.includes('About us') || el.innerText.includes('About'));
                    if (aboutHeader) {
                        let sibling = aboutHeader.nextElementSibling;
                        while (sibling) {
                            if (sibling.tagName.toLowerCase() === 'p') {
                                data.description = sibling.innerText.trim();
                                break;
                            }
                            const p = sibling.querySelector('p');
                            if (p) {
                                data.description = p.innerText.trim();
                                break;
                            }
                            sibling = sibling.nextElementSibling;
                        }
                    }
                    
                    if (!data.description) {
                        const metaDesc = document.querySelector('meta[name="description"]');
                        if (metaDesc) data.description = metaDesc.content;
                    }
                    
                    // 2. Extract key-value fields from dt/dd elements
                    const dts = Array.from(document.querySelectorAll('dt'));
                    for (const dt of dts) {
                        const key = dt.innerText.trim().toLowerCase().replace(/[^a-z0-9]/g, '');
                        const dd = dt.nextElementSibling;
                        if (dd) {
                            let val = dd.innerText.trim();
                            const a = dd.querySelector('a');
                            if (a && a.href) {
                                val = a.href;
                                // Clean up redirect urls
                                if (val.includes('linkedin.com/redir/redirect')) {
                                    try {
                                        const u = new URL(val);
                                        const targetUrl = u.searchParams.get('url');
                                        if (targetUrl) val = targetUrl;
                                    } catch(e) {}
                                }
                            }
                            data[key] = val;
                        }
                    }
                    
                    // 3. Extract Followers count
                    const text = document.body.innerText;
                    const followersMatch = text.match(/([\\d,]+)\\s+followers/i);
                    if (followersMatch) {
                        data.followers = followersMatch[1];
                    }
                    
                    // 4. Extract Employee count
                    const employeesMatch = text.match(/all\\s+(\\d+)\\s+employees/i);
                    if (employeesMatch) {
                        data.employees_on_linkedin = parseInt(employeesMatch[1]);
                    }
                    
                    return data;
                }
                """
            )
            return details
    except Exception as e:
        logger.warning("fetch_company_details_playwright_failed", url=url, error=str(e))
        return {}


async def search_company_employees(company_name: str) -> list[dict[str, str]]:
    """Search for employees/people working at the company using Yahoo search."""
    query = f'site:linkedin.com/in "{company_name}"'
    search_results = await search_yahoo_playwright(query)
    
    employees = []
    for item in search_results[:5]:  # Limit to top 5 employees
        url = item["url"]
        title = item["title"]
        
        # Clean title
        title_lines = [line.strip() for line in title.split("\n") if line.strip()]
        clean_title = title_lines[-1] if title_lines else title
        clean_title = re.sub(r"^(?:Linkedin|LinkedIn)https?://[^\s]+", "", clean_title, flags=re.I).strip()
        clean_title = re.sub(r"^[›\s\-\u00a0]+", "", clean_title).strip()
        
        name = clean_title
        role = "LinkedIn Member"
        
        # Split by - or | or @
        parts = [p.strip() for p in re.split(r"[\-\|]", clean_title) if p.strip()]
        if parts:
            name = parts[0]
            if len(parts) > 1:
                role = " - ".join(parts[1:])
                role = re.sub(r"\s*\|\s*linkedin", "", role, flags=re.I).strip()
                
        employees.append({
            "name": name,
            "role": role,
            "url": url,
        })
    return employees


async def verify_linkedin_profile(
    email: str | None = None,
    company_name: str | None = None,
    verified_emails: list[str] | None = None,
) -> dict[str, Any]:
    """Search and verify a person's LinkedIn profile based on their email or company name.
    
    Returns a dict with verification details.
    """
    result = {
        "verified": False,
        "linkedin_url": None,
        "name": None,
        "role": None,
        "company": None,
        "matched_method": "NONE",
        "snippet": None,
        
        # Advanced company fields
        "followers": None,
        "employees_on_linkedin": None,
        "industry": None,
        "company_size": None,
        "headquarters": None,
        "founded": None,
        "specialties": None,
        "profile_website": None,
        "profile_description": None,
        
        # Employee list
        "employees": [],
    }
    
    if not email and not company_name:
        return result
        
    extracted_name = extract_name_from_email(email) if email else None
    
    # We formulate different search queries to try in order of priority:
    # 1. Search for specific email: site:linkedin.com/in "email@domain.com"
    # 2. Search for extracted name + company: site:linkedin.com/in "First Last" "Company"
    # 3. Search for company page: site:linkedin.com/company "Company"
    
    queries = []
    if email:
        queries.append((f'site:linkedin.com/in "{email}"', "EMAIL"))
    if extracted_name and company_name:
        queries.append((f'site:linkedin.com/in "{extracted_name}" "{company_name}"', "NAME"))
    elif company_name:
        queries.append((f'site:linkedin.com/company {company_name}', "COMPANY"))
        
    for query, method in queries:
        logger.info("linkedin_search_attempt", query=query, method=method)
        
        # Query Yahoo via Playwright
        search_results = await search_yahoo_playwright(query)
        if not search_results:
            continue
            
        # Analyze results
        for item in search_results:
            url = item["url"]
            title = item["title"]
            snippet = item["snippet"]
            
            # Clean title
            # In Yahoo, the title often has Yahoo breadcrumb URL prefix, separated by newlines
            title_lines = [line.strip() for line in title.split("\n") if line.strip()]
            clean_title = title_lines[-1] if title_lines else title
            # Fallback regex if it didn't split by newline
            clean_title = re.sub(r"^(?:Linkedin|LinkedIn)https?://[^\s]+", "", clean_title, flags=re.I).strip()
            # Remove any leading special characters
            clean_title = re.sub(r"^[›\s\-\u00a0]+", "", clean_title).strip()
            
            # Check if this is a valid LinkedIn profile or company page link
            if "linkedin.com/in/" in url and method in ("EMAIL", "NAME"):
                # Clean title to extract name, role, company
                # LinkedIn title format: "John Doe - Software Engineer - Vertex | LinkedIn"
                title_parts = [t.strip() for t in clean_title.split("-")]
                
                result["verified"] = True
                result["linkedin_url"] = url
                result["matched_method"] = method
                result["snippet"] = snippet
                
                if len(title_parts) >= 1:
                    result["name"] = title_parts[0]
                if len(title_parts) >= 2:
                    result["role"] = title_parts[1]
                if len(title_parts) >= 3:
                    # Remove "LinkedIn" from the end if present
                    comp = title_parts[2]
                    comp = re.sub(r"\s*\|\s*linkedin", "", comp, flags=re.I).strip()
                    result["company"] = comp
                    
                logger.info("linkedin_verification_success", url=url, name=result["name"])
                return result
                
            elif "linkedin.com/company/" in url and method == "COMPANY":
                result["verified"] = True
                result["linkedin_url"] = url
                result["matched_method"] = method
                result["snippet"] = snippet
                
                # Extract company name from title
                title_parts = [t.strip() for t in clean_title.split("-")]
                if title_parts:
                    comp_name = title_parts[0]
                    comp_name = re.sub(r"\s*\|\s*linkedin", "", comp_name, flags=re.I).strip()
                    result["company"] = comp_name
                
                # Crawl advanced details from the public page
                logger.info("linkedin_crawling_advanced_details", url=url)
                adv_details = await fetch_company_details_playwright(url)
                if adv_details:
                    result["followers"] = adv_details.get("followers")
                    result["employees_on_linkedin"] = adv_details.get("employees_on_linkedin")
                    result["industry"] = adv_details.get("industry")
                    result["company_size"] = adv_details.get("companysize")
                    result["headquarters"] = adv_details.get("headquarters")
                    result["founded"] = adv_details.get("founded")
                    result["specialties"] = adv_details.get("specialties")
                    result["profile_website"] = adv_details.get("website")
                    result["profile_description"] = adv_details.get("description")
                
                # Crawl employee list
                logger.info("linkedin_crawling_employees", company_name=result["company"])
                raw_employees = await search_company_employees(result["company"])
                
                # Match emails to employees
                matched_employees = []
                emails_list = verified_emails or []
                for emp in raw_employees:
                    emp_name = emp["name"].lower().strip()
                    emp_parts = [p for p in re.split(r"[\s\._\-]", emp_name) if p]
                    
                    matched_email = None
                    for mail in emails_list:
                        mail_lower = mail.lower().strip()
                        mail_prefix = mail_lower.split("@")[0]
                        
                        # 1. Exact match on prefix
                        if mail_prefix == emp_name:
                            matched_email = mail
                            break
                        # 2. Match first name prefix
                        if len(emp_parts) >= 1 and mail_prefix == emp_parts[0]:
                            matched_email = mail
                            break
                        # 3. Match combinations
                        if len(emp_parts) >= 2:
                            c1 = f"{emp_parts[0]}.{emp_parts[1]}"
                            c2 = f"{emp_parts[0]}_{emp_parts[1]}"
                            c3 = f"{emp_parts[0]}{emp_parts[1]}"
                            c4 = f"{emp_parts[0]}{emp_parts[1][0]}"
                            c5 = f"{emp_parts[0]}.{emp_parts[1][0]}"
                            if mail_prefix in (c1, c2, c3, c4, c5):
                                matched_email = mail
                                break
                                
                    matched_employees.append({
                        "name": emp["name"],
                        "role": emp["role"],
                        "url": emp["url"],
                        "email": matched_email,
                    })
                result["employees"] = matched_employees
                    
                logger.info("linkedin_company_verification_success", url=url, company=result["company"])
                return result
                
    return result
