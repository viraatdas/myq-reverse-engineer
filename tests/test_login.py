"""Login flow tests.

These pin down the details MyQ has already changed once and will change again:
where the sign-in form posts, which hidden fields ride along, and how a
rejected password is distinguished from a blocked flow.
"""

from __future__ import annotations

import pytest

from myq import login as login_module
from myq.client import OAUTH_CLIENT_ID
from myq.login import InvalidCredentials, LoginBlocked

LOGIN_PAGE = """
<html><body>
<form action="/Account/LoginWithEmail?returnUrl=%2Fconnect%2Fauthorize" method="post">
  <input id="ReturnUrl" name="ReturnUrl" type="hidden" value="/connect/authorize/callback?x=1" />
  <input id="Brand" name="Brand" type="hidden" value="myq" />
  <input id="UnifiedFlowRequested" name="UnifiedFlowRequested" type="hidden" value="True" />
  <input type="email" id="login-email" name="Email" />
  <input type="password" id="login-password" name="Password" />
  <input name="__RequestVerificationToken" type="hidden" value="CfDJ8-token" />
  <input class="btn" id="submit_button" type="submit" value="Sign In" />
</form>
</body></html>
"""

BAD_PASSWORD_PAGE = """
<html><body><div class="validation-summary-errors" data-valmsg-summary="true">
<ul><li>The password does not match the email provided.</li></ul></div></body></html>
"""

MFA_PAGE = "<html><body><h1>Enter the verification code we sent you</h1></body></html>"
CHALLENGE_PAGE = "<html><head><title>Just a moment...</title></head></html>"


# ---------------- form parsing ----------------


def test_form_action_is_read_from_the_page():
    """Regression: MyQ moved the POST target to /Account/LoginWithEmail.

    Posting to the login page URL instead now returns 405, which is what broke
    the widely-copied implementations.
    """
    action, fields, email_field, password_field = login_module._parse_login_form(LOGIN_PAGE)
    assert action.startswith("/Account/LoginWithEmail")
    assert email_field == "Email"
    assert password_field == "Password"


def test_all_hidden_fields_are_carried_over():
    _, fields, _, _ = login_module._parse_login_form(LOGIN_PAGE)
    assert fields["Brand"] == "myq"
    assert fields["UnifiedFlowRequested"] == "True"
    assert fields["__RequestVerificationToken"] == "CfDJ8-token"
    assert fields["ReturnUrl"].startswith("/connect/authorize/callback")
    # The submit button is not a field to post back.
    assert "submit_button" not in fields


def test_form_without_anti_forgery_token_is_rejected():
    with pytest.raises(LoginBlocked):
        login_module._parse_login_form("<form action='/x'><input name='Email'></form>")


def test_missing_form_is_rejected():
    with pytest.raises(LoginBlocked):
        login_module._parse_login_form("<html><body>nope</body></html>")


# ---------------- error detection ----------------


def test_validation_error_is_extracted():
    msg = login_module._validation_error(BAD_PASSWORD_PAGE)
    assert msg == "The password does not match the email provided."


def test_validation_error_absent_on_a_clean_page():
    assert login_module._validation_error(LOGIN_PAGE) is None


def test_mfa_page_is_detected():
    assert login_module._needs_second_factor(MFA_PAGE)
    assert not login_module._needs_second_factor(LOGIN_PAGE)


def test_cloudflare_challenge_is_detected():
    assert login_module._looks_like_challenge(CHALLENGE_PAGE)
    assert not login_module._looks_like_challenge(LOGIN_PAGE)


# ---------------- token exchange ----------------


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json = json_body or {}
        self.text = text

    def json(self):
        return self._json


def test_exchange_sends_pkce_verifier_and_app_headers(monkeypatch):
    seen = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        seen.update(url=url, data=data, headers=headers)
        return FakeResponse(200, {"access_token": "at", "refresh_token": "rt"})

    monkeypatch.setattr(login_module.httpx, "post", fake_post)
    login_module.exchange_code("CODE", "VERIFIER")

    assert seen["data"]["code_verifier"] == "VERIFIER"
    assert seen["data"]["grant_type"] == "authorization_code"
    assert seen["data"]["client_id"] == OAUTH_CLIENT_ID
    # The Android client authenticates with PKCE + app headers, no secret.
    assert "client_secret" not in seen["data"]
    assert seen["headers"]["MyQApplicationId"]
    assert seen["headers"]["App-Version"]


def test_401_122_is_reported_as_a_rejected_code_not_a_client_problem(monkeypatch):
    """401.122 looks like a client-auth failure but is really a bad code."""
    monkeypatch.setattr(
        login_module.httpx,
        "post",
        lambda *a, **k: FakeResponse(401, text='{"code":"401.122","message":"Unauthorized"}'),
    )
    with pytest.raises(LoginBlocked, match="single-use"):
        login_module.exchange_code("CODE", "VERIFIER")


def test_reused_code_gives_an_actionable_message(monkeypatch):
    monkeypatch.setattr(
        login_module.httpx,
        "post",
        lambda *a, **k: FakeResponse(400, text='{"error":"invalid_grant"}'),
    )
    with pytest.raises(LoginBlocked, match="already used or has expired"):
        login_module.exchange_code("CODE", "VERIFIER")


# ---------------- end-to-end flow ----------------


class FakeClient:
    """Minimal httpx.Client stand-in driving a scripted redirect chain."""

    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _next(self, method, url, **kw):
        self.requests.append((method, url, kw))
        return self.responses.pop(0)

    def get(self, url, **kw):
        return self._next("GET", url, **kw)

    def post(self, url, **kw):
        return self._next("POST", url, **kw)


class FakeHTTPResponse:
    def __init__(self, status_code=200, text="", headers=None, url="https://partner-identity.myq-cloud.com/x"):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.url = url


def _install(monkeypatch, responses):
    client = FakeClient(responses)
    monkeypatch.setattr(login_module.httpx, "Client", lambda **kw: client)
    return client


def test_wrong_password_raises_invalid_credentials(monkeypatch):
    """The specific reason must surface, not a generic failure."""
    _install(
        monkeypatch,
        [
            FakeHTTPResponse(302, headers={"location": "/Account/Login?ReturnUrl=x"}),
            FakeHTTPResponse(200, text=LOGIN_PAGE),
            FakeHTTPResponse(200, text=BAD_PASSWORD_PAGE),
        ],
    )
    with pytest.raises(InvalidCredentials, match="does not match"):
        login_module.fetch_authorization_code("a@b.com", "wrong")


def test_mfa_account_is_directed_to_the_browser_flow(monkeypatch):
    _install(
        monkeypatch,
        [
            FakeHTTPResponse(302, headers={"location": "/Account/Login"}),
            FakeHTTPResponse(200, text=LOGIN_PAGE),
            FakeHTTPResponse(200, text=MFA_PAGE),
        ],
    )
    with pytest.raises(LoginBlocked, match="two-factor"):
        login_module.fetch_authorization_code("a@b.com", "pw")


def test_cloudflare_challenge_is_directed_to_the_browser_flow(monkeypatch):
    _install(monkeypatch, [FakeHTTPResponse(200, text=CHALLENGE_PAGE)])
    with pytest.raises(LoginBlocked, match="Cloudflare"):
        login_module.fetch_authorization_code("a@b.com", "pw")


def test_successful_flow_returns_the_code(monkeypatch):
    client = _install(
        monkeypatch,
        [
            FakeHTTPResponse(302, headers={"location": "/Account/Login?ReturnUrl=x"}),
            FakeHTTPResponse(200, text=LOGIN_PAGE),
            FakeHTTPResponse(302, headers={"location": "/connect/authorize/callback?y=1"}),
            FakeHTTPResponse(302, headers={"location": "com.myqops://android?code=THECODE-1"}),
        ],
    )
    code, verifier = login_module.fetch_authorization_code("a@b.com", "pw")

    assert code == "THECODE-1"
    assert verifier
    # Credentials must go to the form's action, not the page URL.
    method, url, kw = client.requests[2]
    assert method == "POST"
    assert "/Account/LoginWithEmail" in url
    assert kw["data"]["Email"] == "a@b.com"
    assert kw["data"]["Password"] == "pw"
    assert kw["data"]["__RequestVerificationToken"] == "CfDJ8-token"


def test_redirect_chain_without_a_code_is_reported(monkeypatch):
    _install(
        monkeypatch,
        [
            FakeHTTPResponse(302, headers={"location": "/Account/Login"}),
            FakeHTTPResponse(200, text=LOGIN_PAGE),
            FakeHTTPResponse(302, headers={"location": "/somewhere"}),
            FakeHTTPResponse(200, headers={}),
        ],
    )
    with pytest.raises(LoginBlocked, match="without an authorization code"):
        login_module.fetch_authorization_code("a@b.com", "pw")


# ---------------- misc ----------------


def test_extract_code_handles_the_iss_parameter():
    url = "com.myqops://android?code=ABC123-1&iss=https%3A%2F%2Fpartner-identity.myq-cloud.com"
    assert login_module.extract_code(url) == "ABC123-1"


def test_extract_code_rejects_url_without_code():
    with pytest.raises(ValueError):
        login_module.extract_code("com.myqops://android?error=access_denied")


def test_build_auth_url_is_pkce_and_unique():
    url_a, verifier_a = login_module.build_auth_url()
    url_b, verifier_b = login_module.build_auth_url()
    assert "code_challenge_method=S256" in url_a
    assert verifier_a != verifier_b
    assert url_a != url_b
