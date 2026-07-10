"""LinkedIn verification service using Yahoo and DuckDuckGo search."""

import asyncio
import random
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
    """Extract a person's name from their email prefix if it's not a generic email."""
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


async def check_linkedin_login_status(page) -> bool:
    """Check if the browser is logged in to LinkedIn.
    
    Returns False if page is on a login wall, signup screen, or captcha checkpoint.
    """
    current_url = page.url
    if any(x in current_url for x in ("linkedin.com/checkpoint", "linkedin.com/login", "linkedin.com/signup")):
        return False
        
    # Check for login form fields
    try:
        username_el = await page.query_selector("input#username")
        password_el = await page.query_selector("input#password")
        if username_el or password_el:
            return False
    except Exception:
        pass
        
    return True


async def human_like_scroll(page, selector: str | None = None, scrolls: int = 3) -> None:
    """Scroll down a page or container smoothly, mimicking human browsing behavior."""
    for _ in range(scrolls):
        # Generate random step size and micro-pauses
        steps = random.randint(3, 6)
        for _ in range(steps):
            scroll_delta = random.randint(120, 280)
            if selector:
                await page.evaluate(
                    f"""(sel, delta) => {{
                        const el = document.querySelector(sel);
                        if (el) el.scrollBy(0, delta);
                    }}""",
                    selector, scroll_delta
                )
            else:
                await page.evaluate(f"window.scrollBy(0, {scroll_delta})")
            
            # Micro-pause
            await asyncio.sleep(random.uniform(0.15, 0.35))
            
        # Medium pause between scroll sequences
        await asyncio.sleep(random.uniform(0.5, 1.2))


async def search_yahoo_playwright(query: str, offset: int = 1) -> list[dict[str, str]]:
    """Fetch search results from Yahoo using Playwright (no captcha wall)."""
    url = f"https://search.yahoo.com/search?q={urllib.parse.quote(query)}&b={offset}"
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


async def fetch_complete_company_details(url: str) -> dict[str, Any]:
    """Visit the LinkedIn company page (About, Posts, People tabs) to scrape complete details.
    
    Uses your saved login session and mimics human browser interactions.
    """
    # Clean the base company URL (remove any existing tab paths)
    base_url = re.sub(r"/(?:about|posts|people|jobs)/?$", "", url).rstrip("/")
    
    details = {
        "description": None,
        "website": None,
        "industry": None,
        "companysize": None,
        "headquarters": None,
        "founded": None,
        "specialties": None,
        "followers": None,
        "employees_on_linkedin": None,
        "posts": [],
        "employees": [],
    }

    browser = get_browser_manager()
    try:
        async with browser.page() as page:
            # Set evasive headers
            await page.set_extra_http_headers({
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
            })
            
            # --- 1. Crawl About Tab ---
            about_url = f"{base_url}/about/"
            logger.info("navigating_to_linkedin_about_tab", url=about_url)
            await page.goto(about_url, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(random.uniform(2.0, 4.0))

            # Check login status
            is_logged_in = await check_linkedin_login_status(page)
            if not is_logged_in:
                logger.warning("linkedin_not_logged_in_cannot_scrape_company_details", url=about_url)
                print("\n⚠️ WARNING: LinkedIn session is expired or not logged in.")
                print("Please run 'python login_linkedin.py' to log in to your dummy account.\n")
                return {}

            await human_like_scroll(page, scrolls=2)
            await asyncio.sleep(random.uniform(1.0, 2.0))

            try:
                about_data = await page.evaluate(
                    """
                    () => {
                        const data = {};
                        
                        // Overview Description
                        const headers = Array.from(document.querySelectorAll('h2, h3, h4, font, span'));
                        const overviewHeader = headers.find(el => {
                            const txt = el.innerText.trim().toLowerCase();
                            return txt === 'overview' || txt === 'about us' || txt === 'about';
                        });
                        
                        if (overviewHeader) {
                            let sibling = overviewHeader.nextElementSibling;
                            while (sibling) {
                                const tag = sibling.tagName.toLowerCase();
                                if (tag === 'p') {
                                    data.description = sibling.innerText.trim();
                                    break;
                                }
                                const p = sibling.querySelector('p');
                                if (p && p.innerText.trim()) {
                                    data.description = p.innerText.trim();
                                    break;
                                }
                                sibling = sibling.nextElementSibling;
                            }
                        }
                        
                        if (!data.description) {
                            const paragraphs = Array.from(document.querySelectorAll('p.break-words, p.text-body-medium'));
                            if (paragraphs.length > 0) {
                                data.description = paragraphs[0].innerText.trim();
                            }
                        }
                        
                        // Key-value fields from dt/dd
                        const dts = Array.from(document.querySelectorAll('dt'));
                        for (const dt of dts) {
                            const key = dt.innerText.trim().toLowerCase().replace(/[^a-z0-9]/g, '');
                            const dd = dt.nextElementSibling;
                            if (dd) {
                                let val = dd.innerText.trim();
                                const a = dd.querySelector('a');
                                if (a && a.href) {
                                    val = a.href;
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
                        
                        const bodyText = document.body.innerText || "";
                        
                        // Followers Count
                        const followersMatch = bodyText.match(/([\\d,]+)\\s+followers/i);
                        if (followersMatch) {
                            data.followers = followersMatch[1];
                        }
                        
                        // Employees Count
                        const empMatch = bodyText.match(/see all\\s+(\\d[\\d,]*)\\s+employees/i)
                            || bodyText.match(/(\\d[\\d,]*)\\s+employees on linkedin/i)
                            || bodyText.match(/(\\d[\\d,]*)\\s+employees/i);
                        if (empMatch) {
                            data.employees_on_linkedin = parseInt(empMatch[1].replace(/,/g, ''));
                        }

                        // Founded year fallback
                        if (!data.founded) {
                            const foundedMatch = bodyText.match(/founded[:\\s]+(\\d{4})/i)
                                || bodyText.match(/established in (\\d{4})/i)
                                || bodyText.match(/since (\\d{4})/i);
                            if (foundedMatch) data.founded = foundedMatch[1];
                        }
                        
                        return data;
                    }
                    """
                )
                details.update(about_data)
            except Exception as eval_err:
                logger.warning("linkedin_about_details_evaluation_failed", url=about_url, error=str(eval_err))

            # --- 2. Crawl Posts Tab ---
            posts_url = f"{base_url}/posts/"
            logger.info("navigating_to_linkedin_posts_tab", url=posts_url)
            try:
                await page.goto(posts_url, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(random.uniform(2.0, 4.5))
                await human_like_scroll(page, scrolls=3)
                await asyncio.sleep(random.uniform(1.0, 2.0))

                posts_data = await page.evaluate(
                    """
                    () => {
                        const list = [];
                        const updates = document.querySelectorAll('.feed-shared-update-v2, article, .ocg-feed-update');
                        for (const u of updates) {
                            const textEl = u.querySelector('.feed-shared-update-v2__description, .feed-shared-text-view, .update-components-text, .ocg-feed-update__description');
                            const text = textEl ? textEl.innerText.trim() : '';
                            
                            const timeEl = u.querySelector('.feed-shared-actor__sub-description, .update-components-actor__sub-description, .ocg-feed-update__sub-description');
                            const timeText = timeEl ? timeEl.innerText.split('•')[0].trim() : '';
                            
                            const reactionsEl = u.querySelector('.social-details-social-counts__reactions-count, .social-details-social-counts__reactions, .ocg-feed-update__reactions-count');
                            const reactions = reactionsEl ? reactionsEl.innerText.trim() : '0';
                            
                            const commentsEl = u.querySelector('.social-details-social-counts__comments, .social-details-social-counts__comments-count, .ocg-feed-update__comments-count');
                            const comments = commentsEl ? commentsEl.innerText.trim() : '0';
                            
                            if (text) {
                                list.push({ text, date: timeText, likes: reactions, comments });
                            }
                        }
                        return list.slice(0, 5); // Limit to top 5 recent posts
                    }
                    """
                )
                details["posts"] = posts_data
            except Exception as posts_err:
                logger.warning("linkedin_posts_scrape_failed", url=posts_url, error=str(posts_err))

            # --- 3. Crawl People Tab ---
            people_url = f"{base_url}/people/"
            logger.info("navigating_to_linkedin_people_tab", url=people_url)
            try:
                await page.goto(people_url, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(random.uniform(2.5, 5.0))
                await human_like_scroll(page, scrolls=3)
                await asyncio.sleep(random.uniform(1.0, 2.0))

                people_data = await page.evaluate(
                    """
                    () => {
                        const list = [];
                        const cards = Array.from(document.querySelectorAll('.org-people-profile-card, li.org-people-profile-card__profile-card-spacing, .org-people-card'));
                        for (const card of cards) {
                            const link = card.querySelector('a[href*="/in/"]');
                            if (!link) continue;
                            
                            const href = link.href;
                            if (list.some(x => x.url === href)) continue;
                            
                            const nameEl = card.querySelector('.org-people-profile-card__profile-title, .lt-line-clamp--single-line, h2, h3, .artdeco-entity-lockup__title');
                            const name = nameEl ? nameEl.innerText.trim() : '';
                            
                            const roleEl = card.querySelector('.org-people-profile-card__profile-headline, .lt-line-clamp--multi-line, .text-body-small, .artdeco-entity-lockup__subtitle');
                            const role = roleEl ? roleEl.innerText.trim() : '';
                            
                            if (name && name !== 'LinkedIn Member') {
                                list.push({ name, role, url: href });
                            }
                        }
                        return list;
                    }
                    """
                )
                details["employees"] = people_data
            except Exception as people_err:
                logger.warning("linkedin_people_scrape_failed", url=people_url, error=str(people_err))

    except Exception as e:
        logger.warning("fetch_complete_company_details_failed", url=url, error=str(e))
        
    return details


async def search_company_employees(company_name: str) -> list[dict[str, str]]:
    """Search for employees/people working at the company using Yahoo search."""
    query = f'site:linkedin.com/in "{company_name}"'
    
    # Fetch up to 5 pages of Yahoo search results to find as many actual employees as possible
    raw_results = []
    for p_idx in range(5):
        offset = p_idx * 10 + 1
        page_results = await search_yahoo_playwright(query, offset=offset)
        if not page_results:
            break
        # Deduplicate
        for item in page_results:
            if not any(r["url"] == item["url"] for r in raw_results):
                raw_results.append(item)
                
        # Randomized delay between search engine pages to mimic human scrolling/paging
        await asyncio.sleep(random.uniform(2.5, 4.5))

    employees = []
    comp_clean = company_name.lower().strip()
    comp_words = [w for w in re.findall(r'\w+', comp_clean) if w not in ("pvt", "ltd", "gmbh", "co", "corp", "corporation", "inc", "incorporated", "llc", "company", "limited")]
    
    for item in raw_results:
        url = item["url"]
        title = item["title"]
        snippet = item["snippet"]
        
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

        # Check if they actually work at the company
        if name.lower().strip() == comp_clean:
            continue
            
        role_lower = role.lower()
        snippet_lower = snippet.lower()
        
        is_employee = False
        if comp_clean in role_lower or comp_clean in snippet_lower:
            is_employee = True
        elif comp_words:
            for word in comp_words:
                if len(word) > 4 and (word in role_lower or word in snippet_lower):
                    is_employee = True
                    break
                elif len(word) <= 4:
                    pattern = r'\b' + re.escape(word) + r'\b'
                    if re.search(pattern, role_lower) or re.search(pattern, snippet_lower):
                        if word in ("media", "tech", "web", "seo", "ad", "ads"):
                            other_words = [w for w in comp_words if w != word]
                            if any(ow in role_lower or ow in snippet_lower for ow in other_words):
                                is_employee = True
                                break
                        else:
                            is_employee = True
                            break
                            
        if is_employee:
            role_lower = role.lower().strip()
            exclude_keywords = [
                "student", "alumni", "alumnus", "studying", "candidate", "graduate", 
                "pupil", "course", "batch", "learning", "education", "class", 
                "certified", "trainee", "intern", "ex-", "ex ", "former", "retired"
            ]
            if any(kw in role_lower for kw in exclude_keywords):
                continue

            clean_role_check = re.sub(r"\b(?:linkedin|member)\b.*", "", role_lower, flags=re.I).strip()
            clean_role_check = re.sub(r"[\s\-\|›\u00a0\u2010-\u2015]+$", "", clean_role_check).strip()
            if clean_role_check == comp_clean:
                continue

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
        
        # Posts
        "posts": [],
        
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
        
        # Randomized delay between search engine operations to look natural
        await asyncio.sleep(random.uniform(2.0, 4.0))
        
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
            title_lines = [line.strip() for line in title.split("\n") if line.strip()]
            clean_title = title_lines[-1] if title_lines else title
            clean_title = re.sub(r"^(?:Linkedin|LinkedIn)https?://[^\s]+", "", clean_title, flags=re.I).strip()
            clean_title = re.sub(r"^[›\s\-\u00a0]+", "", clean_title).strip()
            
            # Check if this is a valid LinkedIn profile or company page link
            if "linkedin.com/in/" in url and method in ("EMAIL", "NAME"):
                title_parts = [t.strip() for t in re.split(r"[\-\|]", clean_title) if t.strip()]
                title_parts = [p for p in title_parts if p.lower() not in ("linkedin", "member")]
                
                result["verified"] = True
                result["linkedin_url"] = url
                result["matched_method"] = method
                result["snippet"] = snippet
                
                if len(title_parts) >= 1:
                    result["name"] = title_parts[0]
                if len(title_parts) >= 2:
                    result["role"] = title_parts[1]
                if len(title_parts) >= 3:
                    result["company"] = title_parts[2]
                    
                try:
                    safe_name = result["name"].encode("ascii", errors="replace").decode("ascii")
                    logger.info("linkedin_verification_success", url=url, name=safe_name)
                except Exception:
                    pass
                return result
                
            elif "linkedin.com/company/" in url and method == "COMPANY":
                result["verified"] = True
                result["linkedin_url"] = url
                result["matched_method"] = method
                result["snippet"] = snippet
                
                # Extract company name from title
                title_parts = [t.strip() for t in re.split(r"[\-\|]", clean_title) if t.strip()]
                title_parts = [p for p in title_parts if p.lower() not in ("linkedin", "member")]
                if title_parts:
                    result["company"] = title_parts[0]
                else:
                    result["company"] = company_name
                
                # Crawl advanced details from the public/logged-in tabs
                logger.info("linkedin_crawling_advanced_details", url=url)
                adv_details = await fetch_complete_company_details(url)
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
                    result["posts"] = adv_details.get("posts", [])
                
                # Crawl employee list (direct from People tab if available, else Yahoo fallback)
                direct_employees = adv_details.get("employees", []) if adv_details else []
                if direct_employees:
                    logger.info("linkedin_using_direct_scraped_employees", count=len(direct_employees))
                    raw_employees = direct_employees
                else:
                    logger.info("linkedin_crawling_employees_fallback_yahoo", company_name=result["company"])
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
