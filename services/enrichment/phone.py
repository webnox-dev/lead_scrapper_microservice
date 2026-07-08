"""Phone verification utility."""

import re
import phonenumbers
from core.logging import get_logger

logger = get_logger(__name__)


def clean_phone(phone: str, default_region: str = "IN") -> str:
    """Clean and standardize phone number using Google's phonenumbers library.
    
    Returns:
    - 10-digit number for Indian numbers (no +91 prefix) as expected by the DB/UI.
    - E.164 format (e.g. +971543470278) for international numbers.
    """
    if not phone:
        return ""
    try:
        cleaned_input = phone.strip()
        parsed = phonenumbers.parse(cleaned_input, default_region)
        if phonenumbers.is_possible_number(parsed):
            if parsed.country_code == 91:
                return str(parsed.national_number)
            else:
                return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        pass
    
    digits = re.sub(r"\D", "", phone)
    if phone.strip().startswith("+"):
        return f"+{digits}"
    return digits


def verify_phone(phone: str, default_region: str = "IN") -> dict:
    """Verify phone number syntax, validity, type, and carrier possibility.
    
    Returns a dict with verification details:
    {
        "valid": bool,
        "possible": bool,
        "cleaned": str,
        "country_code": int | None,
        "number_type": str,  # MOBILE, FIXED_LINE, TOLL_FREE, etc.
    }
    """
    result = {
        "valid": False,
        "possible": False,
        "cleaned": "",
        "country_code": None,
        "number_type": "UNKNOWN",
    }
    
    if not phone:
        return result
        
    try:
        cleaned = clean_phone(phone, default_region)
        result["cleaned"] = cleaned
        
        parsed = phonenumbers.parse(phone.strip(), default_region)
        result["possible"] = phonenumbers.is_possible_number(parsed)
        result["valid"] = phonenumbers.is_valid_number(parsed)
        result["country_code"] = parsed.country_code
        
        if result["valid"]:
            num_type = phonenumbers.number_type(parsed)
            # Map type integer to string description
            type_mapping = {
                0: "FIXED_LINE",
                1: "MOBILE",
                2: "FIXED_LINE_OR_MOBILE",
                3: "TOLL_FREE",
                4: "PREMIUM_RATE",
                5: "SHARED_COST",
                6: "VOIP",
                7: "PERSONAL_NUMBER",
                8: "PAGER",
                9: "UAN",
                10: "VOICEMAIL",
                -1: "UNKNOWN"
            }
            result["number_type"] = type_mapping.get(num_type, "UNKNOWN")
            
            # Exclude shared cost (like 6-digit 13 numbers), premium rate, and short numbers (<= 8 digits)
            if result["number_type"] in ("SHARED_COST", "PREMIUM_RATE") or len(str(parsed.national_number)) <= 8:
                result["valid"] = False
            
    except Exception as e:
        logger.debug("phone_verification_failed", phone=phone, error=str(e))
        
    return result
