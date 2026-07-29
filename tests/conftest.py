"""Shared fixtures.

API tests run against a stub client so they exercise the HTTP surface —
auth, formatting, idempotency, error mapping — without touching MyQ.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from myq import api as api_module
from myq.client import DoorState
from myq.config import Settings

API_KEY = "test-key-abc123"


class StubClient:
    """Stands in for MyQClient. Records commands, returns scripted states."""

    def __init__(self, state: str = "closed", online: bool = True):
        self.door = DoorState(
            name="Test Door",
            serial_number="SN123",
            state=state,
            online=online,
            last_update="2026-07-29T00:00:00Z",
            last_status="2026-07-29T00:00:00Z",
        )
        self.commands: list[str] = []
        self.tokens = _StubTokens()
        self.store = _StubStore()
        # State the door lands on after a successful command + wait.
        self.final_state: str | None = None
        self.raise_on_state: Exception | None = None

    async def get_door_state(self, serial=None, *, force=False) -> DoorState:
        if self.raise_on_state:
            raise self.raise_on_state
        return self.door

    async def list_doors(self):
        return [self.door]

    async def get_devices(self, *, force=False):
        return [{"name": "Test Door", "device_family": "garagedoor", "serial_number": "SN123"}]

    async def send_command(self, action: str, serial=None) -> DoorState:
        self.commands.append(action)
        return self.door

    async def wait_for_state(self, target, serial=None, timeout=None) -> DoorState:
        self.door.state = self.final_state or target
        return self.door

    async def refresh(self):
        return None

    async def close(self):
        return None


class _StubTokens:
    expires_at = 4102444800.0  # year 2100


class _StubStore:
    location = "file:/tmp/tokens.json"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        api_key=API_KEY,
        token_store="file",
        rate_limit_requests=1000,
        command_poll_interval=0.0,
    )


@pytest.fixture
def stub() -> StubClient:
    return StubClient()


@pytest.fixture
def client(settings, stub) -> TestClient:
    # Seed state before TestClient runs the lifespan, so ensure_state() sees a
    # client already present and does not build a real one.
    api_module.app.state.settings = settings
    api_module.app.state.client = stub
    api_module._rate_state.clear()
    with TestClient(api_module.app) as test_client:
        yield test_client
    api_module.app.state.client = None


@pytest.fixture
def auth() -> dict:
    return {"X-API-Key": API_KEY}
