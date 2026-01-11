"""Configuration management for MyQ Garage API."""

import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache

# Get the directory where this file is located
BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # MyQ Account Credentials
    myq_email: str
    myq_password: str
    
    # API Security
    api_key: str  # Your custom API key for protecting endpoints
    
    # Server Settings
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    
    # MyQ API Settings (these may need updating if MyQ changes their API)
    myq_api_base: str = "https://api.myqdevice.com/api/v5.2"
    myq_auth_base: str = "https://partner-identity.myq-cloud.com"
    
    # iOS App credentials (commonly used for unofficial access)
    # These are the iOS app's OAuth credentials - MyQ may change these
    myq_client_id: str = "IOS_CGI_MYQ"
    myq_client_secret: str = ""  # Often not needed for PKCE flow
    myq_redirect_uri: str = "com.myqops://ios"
    
    # Rate limiting
    rate_limit_requests: int = 10
    rate_limit_window: int = 60  # seconds

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()

