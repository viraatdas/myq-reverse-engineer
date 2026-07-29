"""HTTP surface tests: auth, formatting, idempotency, error mapping."""

from __future__ import annotations

import pytest

from myq import api as api_module
from myq.config import Settings
from myq.errors import DeviceNotFound, ReauthRequired, UpstreamError
from tests.conftest import API_KEY, StubClient


# ---------------- auth ----------------


def test_health_needs_no_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_protected_endpoint_rejects_missing_key(client):
    resp = client.get("/status")
    assert resp.status_code == 401
    # The error body must not echo the configured key back.
    assert API_KEY not in resp.text


def test_protected_endpoint_rejects_wrong_key(client):
    resp = client.get("/status", headers={"X-API-Key": "nope"})
    assert resp.status_code == 401


def test_header_auth_accepted(client, auth):
    assert client.get("/status", headers=auth).status_code == 200


def test_bearer_auth_accepted(client):
    resp = client.get("/status", headers={"Authorization": f"Bearer {API_KEY}"})
    assert resp.status_code == 200


def test_query_param_auth_accepted(client):
    """A Shortcuts URL cannot set headers, so ?key= has to work."""
    assert client.get(f"/status?key={API_KEY}").status_code == 200


def test_fails_closed_when_no_api_key_configured(stub):
    """An unconfigured server must refuse, not run wide open."""
    from fastapi.testclient import TestClient

    api_module.app.state.settings = Settings(api_key="", token_store="file")
    api_module.app.state.client = stub
    api_module._rate_state.clear()
    with TestClient(api_module.app) as unconfigured:
        assert unconfigured.get("/status").status_code == 503
        assert unconfigured.get("/health").json()["status"] == "unconfigured"
    api_module.app.state.client = None


# ---------------- status & formatting ----------------


def test_status_shape(client, auth):
    body = client.get("/status", headers=auth).json()
    assert body["name"] == "Test Door"
    assert body["state"] == "closed"
    assert body["is_closed"] is True
    assert body["is_open"] is False


def test_status_text_format(client, auth):
    resp = client.get("/status?format=text", headers=auth)
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text.strip() == "Test Door is closed"


def test_state_endpoint_returns_bare_token(client, auth):
    """/state feeds a Shortcuts If-comparison, so it must be the bare word."""
    resp = client.get("/state", headers=auth)
    assert resp.text.strip() == "closed"


def test_offline_door_marked_in_text(client, auth, stub):
    stub.door.online = False
    assert "offline" in client.get("/status?format=text", headers=auth).text


# ---------------- commands ----------------


@pytest.mark.parametrize("method", ["get", "post"])
def test_open_works_over_both_verbs(client, auth, stub, method):
    """Shortcuts defaults to GET; curl scripts use POST. Both must work."""
    resp = getattr(client, method)("/open", headers=auth)
    assert resp.status_code == 200
    assert resp.json()["action"] == "open"
    assert stub.commands == ["open"]


def test_close_is_idempotent_when_already_closed(client, auth, stub):
    """Repeat automations must not re-fire a command needlessly."""
    resp = client.post("/close", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert "already closed" in body["message"]
    assert stub.commands == []


def test_force_overrides_idempotency(client, auth, stub):
    client.post("/close?force=true", headers=auth)
    assert stub.commands == ["close"]


def test_command_on_offline_door_is_409(client, auth, stub):
    """Commanding an offline opener does nothing upstream; say so."""
    stub.door.online = False
    resp = client.post("/open", headers=auth)
    assert resp.status_code == 409
    assert stub.commands == []


def test_unconfirmed_command_reports_transitional_state(client, auth):
    body = client.post("/open", headers=auth).json()
    assert body["confirmed"] is False
    assert body["state"] == "opening"


def test_wait_confirms_final_state(client, auth, stub):
    body = client.post("/open?wait=true", headers=auth).json()
    assert body["confirmed"] is True
    assert body["state"] == "open"
    assert body["success"] is True


def test_wait_reports_failure_if_door_settles_wrong(client, auth, stub):
    """A door that reverses must not be reported as a success."""
    stub.final_state = "closed"
    body = client.post("/open?wait=true", headers=auth).json()
    assert body["confirmed"] is True
    assert body["state"] == "closed"
    assert body["success"] is False


def test_toggle_closes_an_open_door(client, auth, stub):
    stub.door.state = "open"
    body = client.post("/toggle", headers=auth).json()
    assert body["action"] == "close"
    assert stub.commands == ["close"]


def test_toggle_opens_a_closed_door(client, auth, stub):
    body = client.post("/toggle", headers=auth).json()
    assert body["action"] == "open"
    assert stub.commands == ["open"]


def test_command_text_format(client, auth):
    resp = client.post("/open?format=text", headers=auth)
    assert resp.text.strip() == "Test Door is opening"


# ---------------- error mapping ----------------


@pytest.mark.parametrize(
    "exc,expected",
    [
        (ReauthRequired(), 503),
        (UpstreamError(), 502),
        (DeviceNotFound(), 404),
    ],
)
def test_typed_errors_map_to_status_codes(client, auth, stub, exc, expected):
    """Callers must be able to tell 're-auth' from 'MyQ is down'."""
    stub.raise_on_state = exc
    resp = client.get("/status", headers=auth)
    assert resp.status_code == expected
    assert resp.json()["error"]


def test_unexpected_error_does_not_leak_internals(settings, stub):
    """An unmapped exception must become an opaque 500, not an error dump."""
    from fastapi.testclient import TestClient

    stub.raise_on_state = RuntimeError("secret-token-abc leaked in message")
    api_module.app.state.settings = settings
    api_module.app.state.client = stub
    api_module._rate_state.clear()
    # raise_server_exceptions=False makes TestClient behave like a real server,
    # which returns the handler's response instead of re-raising.
    with TestClient(api_module.app, raise_server_exceptions=False) as raw:
        resp = raw.get("/status", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 500
    assert resp.json()["error"] == "Internal server error"
    assert "secret-token-abc" not in resp.text
    api_module.app.state.client = None


# ---------------- rate limiting ----------------


def test_rate_limit_returns_429(stub):
    from fastapi.testclient import TestClient

    api_module.app.state.settings = Settings(
        api_key=API_KEY, token_store="file", rate_limit_requests=3
    )
    api_module.app.state.client = stub
    api_module._rate_state.clear()
    with TestClient(api_module.app) as limited:
        codes = [limited.get("/status", headers={"X-API-Key": API_KEY}).status_code for _ in range(5)]
    assert codes.count(200) == 3
    assert codes[-1] == 429
    api_module.app.state.client = None


# ---------------- misc ----------------


def test_index_lists_endpoints(client):
    body = client.get("/").json()
    assert "GET|POST /open" in body["endpoints"]


def test_doors_endpoint(client, auth):
    body = client.get("/doors", headers=auth).json()
    assert body["count"] == 1
    assert body["doors"][0]["serial_number"] == "SN123"


def test_openapi_schema_builds(client):
    """A broken response_model would only surface here."""
    assert client.get("/openapi.json").status_code == 200
