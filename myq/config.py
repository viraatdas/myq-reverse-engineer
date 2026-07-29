"""Application settings, loaded from environment / .env."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime configuration.

    Everything has a safe default except ``api_key``, which is deliberately
    empty so an unconfigured deployment fails closed instead of exposing an
    open garage door to the internet.
    """

    # --- Security ---
    # Empty means "not configured" and every protected endpoint returns 503.
    api_key: str = ""
    # CORS origins. Shortcuts does not need CORS, so the default is none.
    cors_origins: list[str] = []

    # --- Token storage ---
    # "file" for local development, "ssm" for AWS Lambda.
    token_store: Literal["file", "ssm"] = "file"
    tokens_file: Path = BASE_DIR / "myq_tokens.json"
    ssm_parameter: str = "/myq/tokens"
    aws_region: str = "us-east-1"

    # --- MyQ account (only needed for the local interactive login) ---
    myq_email: str = ""
    myq_password: str = ""

    # --- Behaviour ---
    # Devices are cached this long to avoid hammering MyQ on every request.
    device_cache_ttl: float = 10.0
    # Refresh the access token this many seconds before it actually expires.
    token_refresh_skew: float = 300.0
    # Default ceiling for ?wait= confirmation polling. Kept under API Gateway's
    # hard 30s integration timeout so a confirmed command returns a real
    # answer rather than a gateway 504.
    command_wait_timeout: float = 25.0
    command_poll_interval: float = 2.0

    # --- Rate limiting (per client IP, per container) ---
    rate_limit_requests: int = 60
    rate_limit_window: float = 60.0

    # --- Local server ---
    host: str = "0.0.0.0"
    port: int = 8000

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
