"""Application configuration."""

from urllib.parse import quote_plus

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App settings loaded from environment."""
    
    # Database (loaded from .env only - no defaults)
    postgres_user: str
    postgres_password: str
    postgres_host: str
    postgres_port: int
    postgres_db: str
    
    # App
    app_name: str = "Leads Platform v2"
    app_version: str = "1.0.0"
    debug: bool = False
    environment: str = "production"
    secret_key: str = "change-me-in-production"
    api_key_header: str = "X-API-Key"
    
    # Browser
    browser_headless: bool = True
    browser_concurrency: int = 5
    browser_max_pages: int = 8
    browser_restart_interval: int = 100
    browser_channel: str = "chrome"
    
    # Scraping
    search_concurrency: int = 3
    business_concurrency: int = 5
    max_retries: int = 3
    max_pages_per_site: int = 8
    
    @computed_field
    @property
    def database_url(self) -> str:
        """Construct asyncpg database URL from components."""
        # URL-encode the password to handle special characters like @, #, etc.
        encoded_password = quote_plus(self.postgres_password)
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{encoded_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )
    
    @computed_field
    @property
    def database_url_sync(self) -> str:
        """Construct synchronous database URL for Alembic migrations."""
        encoded_password = quote_plus(self.postgres_password)
        return (
            f"postgresql://{self.postgres_user}:{encoded_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
