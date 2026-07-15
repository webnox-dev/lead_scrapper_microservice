"""Phone extraction and verification utilities."""

import re
from typing import Any

import phonenumbers
from core.logging import get_logger

logger = get_logger(__name__)

_NUMBER_TYPE_NAMES = {
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
    -1: "UNKNOWN",
}

# These are not actionable business contacts.
_REJECTED_NUMBER_TYPES = {
    "PREMIUM_RATE",
    "SHARED_COST",
    "PERSONAL_NUMBER",
    "PAGER",
    "VOICEMAIL",
    "UNKNOWN",
}

_PHONE_CONTEXT_PATTERN = re.compile(
    r"\b(?:phone|mobile|telephone|tel|call|contact|whatsapp|fax|office|reach)\b",
    re.IGNORECASE,
)


def _parse_input(phone: str) -> str:
    """Normalize international dialing notation before parsing."""
    raw = phone.strip()
    if raw.startswith("00"):
        return f"+{raw[2:]}"
    return raw


def _has_explicit_country_code(phone: str) -> bool:
    raw = phone.strip()
    return raw.startswith("+") or raw.startswith("00")


def _has_contact_context(context: str) -> bool:
    return bool(_PHONE_CONTEXT_PATTERN.search(context or ""))


def _format_valid_number(parsed: phonenumbers.PhoneNumber) -> str:
    """Keep the existing Indian display format and use E.164 globally."""
    if parsed.country_code == 91:
        return str(parsed.national_number)
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def clean_phone(phone: str, default_region: str | None = None) -> str:
    """Clean and standardize phone number using Google's phonenumbers library.

    Numbers that cannot be parsed and validated are discarded. We never fall
    back to returning arbitrary digits because page IDs and counters often look
    like phone numbers.
    """
    if not phone:
        return ""
    try:
        parsed = phonenumbers.parse(_parse_input(phone), default_region)
        if not phonenumbers.is_possible_number(parsed):
            return ""
        if not phonenumbers.is_valid_number(parsed):
            return ""

        number_type = _NUMBER_TYPE_NAMES.get(phonenumbers.number_type(parsed), "UNKNOWN")
        if number_type in _REJECTED_NUMBER_TYPES:
            return ""
        return _format_valid_number(parsed)
    except Exception:
        return ""


def verify_phone(
    phone: str,
    default_region: str | None = None,
    *,
    source: str = "unknown",
    context: str = "",
) -> dict[str, Any]:
    """Verify phone number syntax, validity, type, and carrier possibility.

    Body-text candidates need either an explicit country code or nearby phone
    context. This prevents arbitrary IDs and counters from being treated as
    contacts while preserving international numbers such as ``+44 ...``.
    
    Returns a dict with verification details:
    {
        "valid": bool,
        "possible": bool,
        "cleaned": str,
        "country_code": int | None,
        "number_type": str,  # MOBILE, FIXED_LINE, TOLL_FREE, etc.
    }
    """
    result: dict[str, Any] = {
        "valid": False,
        "possible": False,
        "cleaned": "",
        "country_code": None,
        "number_type": "UNKNOWN",
    }
    
    if not phone:
        return result

    raw_phone = phone.strip()
    if source in {"body", "cache", "unknown"}:
        if not _has_explicit_country_code(raw_phone) and not _has_contact_context(context):
            return result

    try:
        parsed = phonenumbers.parse(_parse_input(raw_phone), default_region)
        result["possible"] = phonenumbers.is_possible_number(parsed)
        result["valid"] = phonenumbers.is_valid_number(parsed)
        result["country_code"] = parsed.country_code

        if result["valid"]:
            result["number_type"] = _NUMBER_TYPE_NAMES.get(
                phonenumbers.number_type(parsed),
                "UNKNOWN",
            )

            if (
                result["number_type"] in _REJECTED_NUMBER_TYPES
                or len(str(parsed.national_number)) <= 8
            ):
                result["valid"] = False

            if result["valid"]:
                result["cleaned"] = _format_valid_number(parsed)

    except Exception as e:
        logger.debug("phone_verification_failed", phone=phone, error=str(e))
        
    return result
