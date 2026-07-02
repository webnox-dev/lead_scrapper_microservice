"""Lead API routes for Leads Platform v2."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from api.dependencies import DbSession
from db.models.lead import Lead
from core.logging import get_logger

logger = get_logger(__name__)
router = APIRouter()


def detect_country(address: str | None, area: str | None) -> str:
    """Detect country from the actual business address only.
    
    NOTE: We intentionally do NOT use `area` (the search query location)
    because Google Maps often returns local results even when the user
    searches for a different country (e.g. searching 'digital marketing
    agency in united states' still returns Coimbatore businesses).
    """
    if not address:
        return ""
        
    addr = address.lower()
    
    # Indian indicators - check first since most results are Indian
    indian_states = [
        "tamil nadu", "karnataka", "maharashtra", "kerala", "andhra pradesh",
        "telangana", "west bengal", "rajasthan", "uttar pradesh", "gujarat",
        "madhya pradesh", "bihar", "punjab", "haryana", "odisha", "assam",
        "jharkhand", "chhattisgarh", "uttarakhand", "himachal pradesh",
        "goa", "tripura", "meghalaya", "manipur", "nagaland", "mizoram",
        "arunachal pradesh", "sikkim", "delhi", "chandigarh",
    ]
    indian_cities = [
        "mumbai", "chennai", "bangalore", "bengaluru", "hyderabad", "kolkata",
        "pune", "ahmedabad", "jaipur", "coimbatore", "tiruppur", "kochi",
        "lucknow", "surat", "indore", "bhopal", "noida", "gurgaon", "gurugram",
        "thiruvananthapuram", "visakhapatnam", "nagpur", "thane", "patna",
        "vadodara", "ludhiana", "agra", "madurai", "varanasi", "erode",
        "salem", "tiruchirappalli", "trichy", "mysuru", "mysore",
    ]
    # Indian PIN codes (6 digits)
    import re
    if re.search(r'\b\d{6}\b', addr):
        return "India"
    if any(s in addr for s in indian_states):
        return "India"
    if any(c in addr for c in indian_cities):
        return "India"
    if "india" in addr:
        return "India"
    
    # US indicators
    us_states = [
        "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
        "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
        "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
        "maine", "maryland", "massachusetts", "michigan", "minnesota",
        "mississippi", "missouri", "montana", "nebraska", "nevada",
        "new hampshire", "new jersey", "new mexico", "new york",
        "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
        "pennsylvania", "rhode island", "south carolina", "south dakota",
        "tennessee", "texas", "utah", "vermont", "virginia", "washington",
        "west virginia", "wisconsin", "wyoming",
    ]
    # US ZIP codes (5 digits or 5+4)
    if re.search(r'\b\d{5}(?:-\d{4})?\b', addr) and not re.search(r'\b\d{6}\b', addr):
        return "United States"
    if any(x in addr for x in ["united states", "usa", " us ", "u.s.a"]):
        return "United States"
    if any(s in addr for s in us_states):
        return "United States"
        
    # Other countries
    if any(x in addr for x in ["united kingdom", "london", "great britain", "england", "scotland", "wales"]):
        return "United Kingdom"
    if "canada" in addr:
        return "Canada"
    if "germany" in addr or "deutschland" in addr:
        return "Germany"
    if "australia" in addr:
        return "Australia"
    if "singapore" in addr:
        return "Singapore"
    if "uae" in addr or "dubai" in addr or "emirates" in addr:
        return "United Arab Emirates"
        
    # If we can't determine, return empty (don't guess)
    return ""


def _extract_city_from_address(address: str | None) -> str | None:
    """Extract the city name from a full address string.
    
    Parses comma-separated address parts and returns the most likely
    city component (skipping street details and postal codes).
    """
    if not address:
        return None
    
    import re
    parts = [p.strip() for p in address.split(",") if p.strip()]
    
    # Walk backwards through parts — city is usually before state/country
    # Skip parts that look like postal codes, country names, or state names
    skip_patterns = re.compile(
        r'^\d{5,6}$|^\d{5}-\d{4}$|^india$|^united states$|^usa$|^uk$|^canada$|^australia$',
        re.IGNORECASE,
    )
    
    for part in reversed(parts):
        clean = part.strip()
        # Remove trailing postal codes embedded in the part (e.g. "Tamil Nadu 641025")
        clean_check = re.sub(r'\s+\d{5,6}$', '', clean).strip()
        if skip_patterns.match(clean_check):
            continue
        # Skip state names (these are typically the second-to-last part)
        # But a city is also fine — return the first non-skip part from the end
        # that doesn't look like a street address (has "road", "floor", "no.")
        lower = clean_check.lower()
        if any(x in lower for x in ["floor", "road", " rd", "no.", "plot", "street", " st,", "block", "sector", "nagar"]):
            continue
        return clean_check
    
    # Fallback: return second-to-last part if available
    if len(parts) >= 2:
        return parts[-2].strip()
    return None


@router.delete("/{lead_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_lead(
    lead_id: UUID,
    db: DbSession,
) -> None:
    """Delete a single enriched lead by its ID."""
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lead not found",
        )

    await db.delete(lead)
    await db.commit()
    logger.info("lead_deleted", lead_id=lead_id)


@router.post("/convert-to-client", response_model=list)
async def convert_to_client(
    lead_ids: list[UUID],
    db: DbSession,
) -> list:
    """Batch fetch leads by their IDs for converting/importing to client."""
    if not lead_ids:
        return []

    query = select(Lead).where(Lead.id.in_(lead_ids))
    result = await db.execute(query)
    leads = result.scalars().all()

    return [
        {
            "id": str(lead.id),
            "name": lead.company_name,
            "phone": lead.phones[0] if lead.phones else None,
            "email": lead.emails[0] if lead.emails else None,
            "source": "Scraper v2",
            "address": lead.address,
            "website": lead.website,
            "all_emails": lead.emails,
            "all_phones": lead.phones,
            "social_links": lead.social_links,
        }
        for lead in leads
    ]


from sqlalchemy import func
from sqlalchemy.orm import selectinload
from fastapi import Query

@router.get("", response_model=dict)
async def list_leads(
    db: DbSession,
    job_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    """List enriched leads with pagination and filtering."""
    query = select(Lead)
    count_query = select(func.count(Lead.id))

    if job_id:
        try:
            job_uuid = UUID(job_id)
            query = query.where(Lead.job_id == job_uuid)
            count_query = count_query.where(Lead.job_id == job_uuid)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid job_id UUID")

    # Order by company name
    query = query.order_by(Lead.company_name).offset(offset).limit(limit).options(selectinload(Lead.collection))
    
    result = await db.execute(query)
    leads = result.scalars().all()

    count_result = await db.execute(count_query)
    total = count_result.scalar() or 0

    items = []
    for lead in leads:
        social_links_dict = {}
        if lead.social_links:
            for link in lead.social_links:
                link_lower = link.lower()
                if "facebook.com" in link_lower:
                    social_links_dict["facebook"] = link
                elif "instagram.com" in link_lower:
                    social_links_dict["instagram"] = link
                elif "linkedin.com" in link_lower:
                    social_links_dict["linkedin"] = link
                elif "twitter.com" in link_lower or "x.com" in link_lower:
                    social_links_dict["twitter"] = link
                elif "youtube.com" in link_lower:
                    social_links_dict["youtube"] = link

        items.append({
            "id": str(lead.id),
            "job_id": str(lead.job_id),
            "business_name": lead.company_name,
            "address": lead.address,
            "city": _extract_city_from_address(lead.address),
            "state": None,
            "country": detect_country(lead.address, None),
            "category": lead.collection.keyword if lead.collection else None,
            "rating": lead.collection.rating if lead.collection else None,
            "review_count": lead.collection.review_count if lead.collection else None,
            "website": lead.website,
            "website_status": "good" if lead.website else "none",
            "has_website": bool(lead.website),
            "has_ssl": True if lead.website and lead.website.startswith("https") else False,
            "is_mobile_friendly": True,
            "is_free_hosting": False,
            "emails": lead.emails,
            "phones": lead.phones,
            "whatsapp": lead.whatsapp_numbers[0] if lead.whatsapp_numbers else None,
            "social_links": social_links_dict,
            "provider": "google_maps",
            "source_url": lead.collection.google_maps_id if lead.collection else None,
            "listing_url": lead.collection.google_maps_id if lead.collection else None,
            "quality_score": 80 if lead.emails or lead.phones else 40,
            "quality_label": "high" if lead.emails and lead.phones else "medium" if lead.emails or lead.phones else "low",
            "is_duplicate": False,
            "validation_flags": [],
            "contacts": [],
            "created_at": "",
            "updated_at": "",
        })

    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }
