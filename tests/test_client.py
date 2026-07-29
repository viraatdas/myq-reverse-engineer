"""MyQClient tests against a fake aiohttp session.

Covers the behaviour that actually breaks in production: token refresh,
the 401 retry path, and turning upstream failures into typed errors.
"""

from __future__ import annotations

import json
import time

import pytest
from multidict import CIMultiDict

from myq.client import MyQClient
from myq.config import Settings
from myq.errors import DeviceNotFound, ReauthRequired, UpstreamError
from myq.tokens import Tokens


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status = status
        self._body = json.dumps(body).encode() if body is not None else b""
        self.headers = CIMultiDict(headers or {})

    async def read(self):
        return self._body

    async def text(self):
        return self._body.decode()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Returns queued responses and records every call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def _next(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected extra request: {method} {url}")
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    def request(self, method, url, **kwargs):
        return self._next(method, url, **kwargs)

    def post(self, url, **kwargs):
        return self._next("POST", url, **kwargs)

    async def close(self):
        self.closed = True


DEVICES_BODY = {
    "items": [
        {
            "name": "Garage",
            "device_family": "garagedoor",
            "serial_number": "SN1",
            "state": {"door_state": "closed", "online": True},
        }
    ]
}


def build_client(responses, *, expires_in=1800, refresh_token="rt") -> MyQClient:
    class MemStore:
        location = "mem:test"

        def __init__(self):
            self.saved = None

        def load(self):
            return Tokens(
                access_token="at",
                refresh_token=refresh_token,
                expires_at=time.time() + expires_in,
                account_id="acct",
                device_serial="SN1",
            )

        def save(self, tokens):
            self.saved = tokens

    client = MyQClient(MemStore(), Settings(api_key="k", token_store="file"))
    session = FakeSession(responses)
    client._session = session
    # Pretend the session belongs to the running loop so it is not rebuilt.
    client._session_loop = None
    client._get_session = _returning(session)
    return client


def _returning(value):
    async def _get():
        return value

    return _get


async def test_get_door_state_parses_upstream():
    client = build_client([FakeResponse(200, DEVICES_BODY)])
    state = await client.get_door_state()
    assert state.name == "Garage"
    assert state.state == "closed"
    assert state.is_closed and not state.is_open
    assert state.online


async def test_expired_token_triggers_refresh_before_request():
    """A stale token must be refreshed proactively, not after a 401."""
    refresh = FakeResponse(200, {"access_token": "new-at", "expires_in": 1800})
    client = build_client([refresh, FakeResponse(200, DEVICES_BODY)], expires_in=-10)
    await client.get_door_state()

    assert client._tokens.access_token == "new-at"
    assert client.store.saved is not None  # refresh was persisted
    assert client.calls_to_token_endpoint == 1


async def test_401_triggers_one_refresh_and_replay():
    client = build_client(
        [
            FakeResponse(401),
            FakeResponse(200, {"access_token": "new-at", "expires_in": 1800}),
            FakeResponse(200, DEVICES_BODY),
        ]
    )
    state = await client.get_door_state()
    assert state.serial_number == "SN1"
    assert client._tokens.access_token == "new-at"


async def test_repeated_401_gives_up_with_reauth_required():
    """Two 401s in a row means the session is dead — say so, don't loop."""
    client = build_client(
        [
            FakeResponse(401),
            FakeResponse(200, {"access_token": "new-at", "expires_in": 1800}),
            FakeResponse(401),
        ]
    )
    with pytest.raises(ReauthRequired):
        await client.get_door_state()


async def test_dead_refresh_token_is_reauth_not_upstream_error():
    """A rejected refresh token is unrecoverable; retrying will not help."""
    client = build_client([FakeResponse(400, {"error": "invalid_grant"})], expires_in=-10)
    with pytest.raises(ReauthRequired):
        await client.get_door_state()


async def test_missing_refresh_token_is_reauth():
    client = build_client([], expires_in=-10, refresh_token="")
    with pytest.raises(ReauthRequired):
        await client.get_door_state()


async def test_upstream_500_becomes_upstream_error():
    client = build_client([FakeResponse(500, {"error": "boom"})])
    with pytest.raises(UpstreamError):
        await client.get_door_state()


async def test_upstream_429_becomes_upstream_error():
    client = build_client([FakeResponse(429)])
    with pytest.raises(UpstreamError):
        await client.get_door_state()


async def test_account_without_garage_door_raises_device_not_found():
    client = build_client([FakeResponse(200, {"items": [{"device_family": "gateway"}]})])
    with pytest.raises(DeviceNotFound):
        await client.get_door_state()


async def test_explicit_unknown_serial_raises_rather_than_falling_back():
    """Multi-door safety: never silently operate a different door."""
    client = build_client([FakeResponse(200, DEVICES_BODY)])
    with pytest.raises(DeviceNotFound):
        await client.get_door_state("SN-does-not-exist")


async def test_devices_are_cached_between_calls():
    """One command should not cost two device lookups."""
    client = build_client([FakeResponse(200, DEVICES_BODY)])
    await client.get_door_state()
    await client.get_door_state()  # served from cache; no second response queued
    assert len(client._session.calls) == 1


async def test_force_bypasses_the_device_cache():
    client = build_client([FakeResponse(200, DEVICES_BODY), FakeResponse(200, DEVICES_BODY)])
    await client.get_door_state()
    await client.get_door_state(force=True)
    assert len(client._session.calls) == 2


async def test_command_targets_the_gdo_host():
    """Commands go to a different host than reads, with the CF cookie."""
    client = build_client([FakeResponse(200, DEVICES_BODY), FakeResponse(202)])
    client._tokens = client.store.load()
    client._tokens.cf_cookie = "__cf_bm=abc"
    await client.send_command("open")

    method, url, kwargs = client._session.calls[-1]
    assert method == "PUT"
    assert "account-devices-gdo.myq-cloud.com" in url
    assert url.endswith("/door_openers/SN1/open")
    assert kwargs["headers"]["Cookie"] == "__cf_bm=abc"


async def test_cloudflare_cookie_is_captured_and_persisted():
    client = build_client(
        [FakeResponse(200, DEVICES_BODY, headers={"Set-Cookie": "__cf_bm=xyz; Path=/; Secure"})]
    )
    await client.get_door_state()
    assert client._tokens.cf_cookie == "__cf_bm=xyz"
    assert client.store.saved.cf_cookie == "__cf_bm=xyz"


async def test_invalid_action_rejected():
    client = build_client([])
    with pytest.raises(ValueError):
        await client.send_command("explode")


# Small helper so the refresh assertion above reads clearly.
def _count_token_calls(self) -> int:
    return sum(1 for method, url, _ in self._session.calls if "connect/token" in url)


MyQClient.calls_to_token_endpoint = property(_count_token_calls)
