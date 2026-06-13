"""Security utilities."""

import hmac
from core.config import settings


def verify_api_key(provided_key: str) -> bool:
    """Verify API key using constant-time comparison."""
    expected = settings.secret_key
    return hmac.compare_digest(provided_key.encode(), expected.encode())
