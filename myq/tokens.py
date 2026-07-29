"""Token model and pluggable storage backends.

The original design kept tokens in a JSON file on disk. That works locally but
breaks on any ephemeral host: MyQ access tokens live ~30 minutes, so a
refreshed token written to a container filesystem is lost on the next cold
start and the refresh token eventually goes stale. ``SSMTokenStore`` keeps the
rotating credentials in AWS SSM Parameter Store instead, so refreshes persist.
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)


@dataclass
class Tokens:
    """A MyQ OAuth session plus the account/device ids we resolved from it."""

    access_token: str
    refresh_token: str
    expires_at: float
    account_id: str = ""
    device_serial: str = ""
    # Cloudflare bot-management cookie. MyQ's command host rejects requests
    # without a recent one, so we persist whatever it hands back.
    cf_cookie: str = ""
    scope: str = "MyQ_Residential offline_access"

    def is_expired(self, skew: float = 0.0) -> bool:
        return time.time() >= (self.expires_at - skew)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Tokens":
        if not data.get("access_token"):
            raise ValueError("token payload has no access_token")
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", ""),
            # Tolerate payloads pasted straight out of a proxy capture, which
            # carry expires_in (relative) rather than expires_at (absolute).
            expires_at=float(
                data.get("expires_at")
                or time.time() + float(data.get("expires_in", 1800))
            ),
            account_id=data.get("account_id", ""),
            device_serial=data.get("device_serial", ""),
            cf_cookie=data.get("cf_cookie", ""),
            scope=data.get("scope") or data.get("token_scope") or "MyQ_Residential offline_access",
        )

    def redacted(self) -> dict:
        """Safe-to-log summary."""
        return {
            "account_id": self.account_id,
            "device_serial": self.device_serial,
            "expires_in": round(self.expires_at - time.time()),
            "has_refresh_token": bool(self.refresh_token),
        }


class TokenStore(ABC):
    """Load and persist the rotating MyQ session."""

    @abstractmethod
    def load(self) -> Tokens | None:
        ...

    @abstractmethod
    def save(self, tokens: Tokens) -> None:
        ...

    @property
    @abstractmethod
    def location(self) -> str:
        """Human-readable description, for diagnostics."""


class FileTokenStore(TokenStore):
    """JSON file on local disk. Used for development and the login flow."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> Tokens | None:
        if not self.path.exists():
            return None
        try:
            return Tokens.from_dict(json.loads(self.path.read_text()))
        except (ValueError, OSError) as exc:
            log.warning("Could not read tokens from %s: %s", self.path, exc)
            return None

    def save(self, tokens: Tokens) -> None:
        # Write-then-rename so a crash mid-write cannot truncate the tokens.
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(tokens.to_dict(), indent=2))
        tmp.replace(self.path)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @property
    def location(self) -> str:
        return f"file:{self.path}"


class SSMTokenStore(TokenStore):
    """AWS SSM Parameter Store (SecureString), KMS-encrypted at rest.

    Reads are cached in-process for the life of the Lambda container so a warm
    invocation does not pay an SSM round trip; writes always go through.
    """

    def __init__(self, parameter: str, region: str):
        self.parameter = parameter
        self.region = region
        self._client = None
        self._cache: Tokens | None = None

    @property
    def client(self):
        if self._client is None:
            import boto3  # imported lazily: not needed for local file storage

            self._client = boto3.client("ssm", region_name=self.region)
        return self._client

    def load(self) -> Tokens | None:
        if self._cache is not None:
            return self._cache
        try:
            resp = self.client.get_parameter(Name=self.parameter, WithDecryption=True)
        except Exception as exc:  # ParameterNotFound and friends
            if type(exc).__name__ == "ParameterNotFound":
                log.warning("SSM parameter %s does not exist yet", self.parameter)
            else:
                log.warning("Could not read %s from SSM: %s", self.parameter, exc)
            return None
        try:
            self._cache = Tokens.from_dict(json.loads(resp["Parameter"]["Value"]))
        except ValueError as exc:
            log.warning("SSM parameter %s holds an invalid token payload: %s", self.parameter, exc)
            return None
        return self._cache

    def save(self, tokens: Tokens) -> None:
        self.client.put_parameter(
            Name=self.parameter,
            Value=json.dumps(tokens.to_dict()),
            Type="SecureString",
            Overwrite=True,
        )
        self._cache = tokens

    @property
    def location(self) -> str:
        return f"ssm:{self.parameter}@{self.region}"


def build_token_store(settings: Settings) -> TokenStore:
    if settings.token_store == "ssm":
        return SSMTokenStore(settings.ssm_parameter, settings.aws_region)
    return FileTokenStore(settings.tokens_file)
