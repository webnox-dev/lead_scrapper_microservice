"""API dependencies."""

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.security import verify_api_key
from db.session import AsyncSessionLocal

api_key_header = APIKeyHeader(name=settings.api_key_header, auto_error=False)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Get database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def verify_api_key_dependency(
    request: Request,
    api_key: str = Depends(api_key_header),
) -> None:
    """Verify API key."""
    # Skip auth for health and WebSocket endpoints
    path = request.url.path
    if path.startswith("/health") or "/ws/" in path:
        return

    # Skip verification in development mode or if using default insecure secret key
    if settings.environment == "development" or settings.secret_key == "change-me-in-production":
        return

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required",
        )

    if not verify_api_key(api_key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )


DbSession = Annotated[AsyncSession, Depends(get_db_session)]
VerifyAPIKey = Depends(verify_api_key_dependency)
