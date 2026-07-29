"""MyQ login.

Two paths:

* ``automatic_login`` drives the whole OAuth flow over HTTP — no browser, no
  copy-paste. This is the normal path.
* ``interactive_login`` hands the sign-in page to a real browser and asks for
  the redirect URL back. Needed only when the account has MFA or federated
  sign-in, or if MyQ starts serving a Cloudflare challenge to plain clients.

Notes for whoever maintains this next, because MyQ changes it without warning:

* The login form's POST target is read from the page rather than assumed. MyQ
  moved it from the login page URL itself to ``/Account/LoginWithEmail``, which
  is what broke the widely-copied implementations — posting to the old target
  now returns 405.
* Hidden fields (``ReturnUrl``, ``Brand``, ``UnifiedFlowRequested``,
  ``__RequestVerificationToken``) are collected generically from the rendered
  form, so an added field keeps working without a code change.
* The token endpoint answers a bad authorization code with a non-standard
  ``401 {"code":"401.122"}`` rather than ``400 invalid_grant``. It is not a
  client-authentication failure, so do not "fix" it by adding a client secret.

This module is never bundled into Lambda; it only runs on a workstation.
"""

from __future__ import annotations

import base64
import hashlib
import html as htmlmod
import re
import secrets
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .client import (
    API_BASE,
    API_USER_AGENT,
    APP_VERSION,
    MYQ_APP_ID,
    OAUTH_AUTHORIZE_URI,
    OAUTH_CLIENT_ID,
    OAUTH_CLIENT_SECRET,
    OAUTH_REDIRECT_URI,
    OAUTH_TOKEN_URI,
)
from .errors import MyQError
from .tokens import TokenStore, Tokens

SCOPE = "MyQ_Residential offline_access"

# The login pages are served to a browser; the API expects the app's identity.
LOGIN_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 11; sdk_gphone_x86) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/83.0.4103.106 Mobile Safari/537.36"
)


class InvalidCredentials(MyQError):
    """MyQ rejected the email/password pair."""

    status = 401
    message = "MyQ rejected your email or password"


class LoginBlocked(MyQError):
    """The automated flow cannot continue — MFA, SSO, or a bot challenge."""

    status = 503
    message = "Automated login is not possible for this account"


# ---------------------------------------------------------------- helpers ----


def generate_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return verifier, challenge


def build_auth_url() -> tuple[str, str]:
    """Return ``(auth_url, code_verifier)`` for a fresh PKCE login."""
    verifier, challenge = generate_pkce()
    params = {
        "acr_values": "unified_flow:v1  brand:myq",
        "client_id": OAUTH_CLIENT_ID,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "login",
        "ui_locales": "en-US",
        "redirect_uri": OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
    }
    return f"{OAUTH_AUTHORIZE_URI}?{urlencode(params)}", verifier


def _login_headers(extra: dict | None = None) -> dict:
    headers = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": LOGIN_USER_AGENT,
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "upgrade-insecure-requests": "1",
    }
    if extra:
        headers.update(extra)
    return headers


def _api_headers(extra: dict | None = None) -> dict:
    headers = {
        "Accept-Encoding": "gzip",
        "App-Version": APP_VERSION,
        "BrandId": "1",
        "MyQApplicationId": MYQ_APP_ID,
        "User-Agent": API_USER_AGENT,
    }
    if extra:
        headers.update(extra)
    return headers


def _attr(tag: str, name: str) -> str | None:
    match = re.search(rf'{name}\s*=\s*"([^"]*)"', tag, re.I)
    return htmlmod.unescape(match.group(1)) if match else None


def _parse_login_form(page_html: str) -> tuple[str, dict, str, str]:
    """Extract ``(action, fields, email_field, password_field)`` from the form.

    Read from the rendered HTML rather than hardcoded: MyQ has changed both the
    action and the field set before.
    """
    form = re.search(r"<form[^>]*>", page_html, re.I)
    if not form:
        raise LoginBlocked("The MyQ sign-in page had no login form")
    action = _attr(form.group(0), "action")
    if not action:
        raise LoginBlocked("The MyQ sign-in form had no action target")

    fields: dict[str, str] = {}
    email_field = password_field = None
    for tag in re.findall(r"<input[^>]*>", page_html, re.I):
        name = _attr(tag, "name")
        kind = (_attr(tag, "type") or "text").lower()
        if kind == "email":
            email_field = name or "Email"
        elif kind == "password":
            password_field = name or "Password"
        elif name and kind in ("hidden", "text"):
            fields[name] = _attr(tag, "value") or ""

    if "__RequestVerificationToken" not in fields:
        raise LoginBlocked("The MyQ sign-in form had no anti-forgery token")

    return action, fields, email_field or "Email", password_field or "Password"


def _validation_error(page_html: str) -> str | None:
    """Pull MyQ's own error text out of a re-rendered login page."""
    flat = re.sub(r"\s+", " ", page_html)
    block = re.search(
        r'validation-summary-errors.*?<ul>(.*?)</ul>|field-validation-error[^>]*>(.*?)<', flat, re.I
    )
    if not block:
        return None
    raw = block.group(1) or block.group(2) or ""
    text = htmlmod.unescape(re.sub(r"<[^>]+>", " ", raw)).strip()
    return re.sub(r"\s+", " ", text) or None


def _looks_like_challenge(page_html: str) -> bool:
    return any(
        marker in page_html
        for marker in ("Just a moment", "challenge-platform", "Verify you are human")
    )


def _needs_second_factor(page_html: str) -> bool:
    lowered = page_html.lower()
    return any(
        marker in lowered
        for marker in ("verification code", "two-factor", "two factor", "authenticator app")
    )


# ------------------------------------------------------------ token calls ----


def exchange_code(code: str, verifier: str) -> dict:
    """Trade the authorization code for an access + refresh token pair.

    The MyQ Android client sends no client secret here — PKCE plus the app
    headers is the whole of it. A secret is sent on a retry only because a
    future MyQ change to confidential-client enforcement would otherwise look
    like an unexplained 401.
    """
    payload = {
        "client_id": OAUTH_CLIENT_ID,
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": OAUTH_REDIRECT_URI,
        "scope": SCOPE,
    }
    headers = _api_headers({"Content-Type": "application/x-www-form-urlencoded"})

    resp = httpx.post(OAUTH_TOKEN_URI, data=payload, headers=headers, timeout=30)
    if resp.status_code == 401:
        resp = httpx.post(
            OAUTH_TOKEN_URI,
            data={**payload, "client_secret": OAUTH_CLIENT_SECRET},
            headers=headers,
            timeout=30,
        )

    if resp.status_code == 200:
        return resp.json()

    detail = resp.text[:300]
    # 401.122 here means MyQ would not honour the code, not that the client is
    # unauthenticated — codes are single-use and expire quickly.
    if resp.status_code == 401 and "401.122" in detail:
        raise LoginBlocked(
            "MyQ rejected the authorization code. Codes are single-use and "
            "expire within about a minute, so a hand-copied one usually fails "
            "— prefer `myq login` (automatic) over pasting a redirect URL."
        )
    if resp.status_code == 400 and "invalid_grant" in detail:
        raise LoginBlocked(
            "The authorization code was already used or has expired. "
            "Start the login again to get a fresh one."
        )
    raise LoginBlocked(f"Token exchange failed: {resp.status_code} {detail}")


def resolve_account(access_token: str) -> tuple[str, str, str]:
    """Look up ``(account_id, device_serial, door_name)`` for a new token."""
    headers = _api_headers({"Authorization": f"Bearer {access_token}"})
    with httpx.Client(headers=headers, timeout=30) as client:
        resp = client.get(f"{API_BASE}/api/v6.2/Accounts")
        resp.raise_for_status()
        accounts = resp.json().get("items", [])
        if not accounts:
            raise LoginBlocked("This MyQ login has no accounts attached")
        account_id = accounts[0].get("id", "")

        resp = client.get(f"{API_BASE}/api/v6.2/Accounts/{account_id}/Devices")
        resp.raise_for_status()
        for device in resp.json().get("items", []):
            if device.get("device_family") == "garagedoor":
                return (
                    account_id,
                    device.get("serial_number", ""),
                    device.get("name", "Garage Door"),
                )
    return account_id, "", ""


def tokens_from_response(payload: dict, account_id: str, device_serial: str) -> Tokens:
    return Tokens(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token", ""),
        expires_at=time.time() + float(payload.get("expires_in", 1800)),
        account_id=account_id,
        device_serial=device_serial,
        scope=payload.get("scope", SCOPE),
    )


def _persist(payload: dict, store: TokenStore) -> Tokens:
    account_id, serial, name = resolve_account(payload["access_token"])
    tokens = tokens_from_response(payload, account_id, serial)
    store.save(tokens)
    if name:
        print(f"Found garage door: {name} ({serial})")
    return tokens


# -------------------------------------------------------- automatic login ----


def fetch_authorization_code(email: str, password: str) -> tuple[str, str]:
    """Run the OAuth flow over HTTP and return ``(code, code_verifier)``."""
    auth_url, verifier = build_auth_url()

    with httpx.Client(follow_redirects=False, timeout=30) as client:
        # 1. Authorize endpoint redirects to the hosted sign-in page.
        resp = client.get(auth_url, headers=_login_headers())
        if _looks_like_challenge(resp.text):
            raise LoginBlocked(
                "Cloudflare is challenging automated sign-in. Use `myq login --browser`."
            )
        location = resp.headers.get("location")
        if not location:
            raise LoginBlocked(f"Authorize endpoint returned {resp.status_code}, expected a redirect")

        # 2. Load the sign-in page and read its form.
        page = client.get(
            str(httpx.URL(str(resp.url)).join(location)), headers=_login_headers()
        )
        if _looks_like_challenge(page.text):
            raise LoginBlocked(
                "Cloudflare is challenging automated sign-in. Use `myq login --browser`."
            )
        action, fields, email_field, password_field = _parse_login_form(page.text)
        fields[email_field] = email
        fields[password_field] = password

        # 3. Submit credentials to the form's own action.
        post_url = str(httpx.URL(str(page.url)).join(action))
        submitted = client.post(
            post_url,
            data=fields,
            headers=_login_headers(
                {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "cache-control": "max-age=0",
                    "origin": "https://partner-identity.myq-cloud.com",
                    "referer": str(page.url),
                    "sec-fetch-site": "same-origin",
                    "sec-fetch-user": "?1",
                }
            ),
        )

        # A 200 means the page re-rendered, which means it did not accept us.
        if submitted.status_code == 200:
            if _needs_second_factor(submitted.text):
                raise LoginBlocked(
                    "This account uses two-factor authentication. "
                    "Use `myq login --browser` instead."
                )
            message = _validation_error(submitted.text)
            if message:
                raise InvalidCredentials(f"MyQ said: {message}")
            raise LoginBlocked("MyQ rejected the sign-in for an unrecognised reason")

        location = submitted.headers.get("location")
        if not location:
            raise LoginBlocked(f"Sign-in returned {submitted.status_code} with no redirect")

        # 4. Follow the chain until the app-scheme redirect carries the code.
        url = str(httpx.URL(str(submitted.url)).join(location))
        for _ in range(8):
            if url.startswith("com.myqops://"):
                code = parse_qs(urlsplit(url).query).get("code", [""])[0]
                if not code:
                    raise LoginBlocked("The final redirect carried no authorization code")
                return code, verifier

            hop = client.get(url, headers=_login_headers({"sec-fetch-site": "same-origin"}))
            nxt = hop.headers.get("location")
            if not nxt:
                raise LoginBlocked("The sign-in redirect chain ended without an authorization code")
            url = nxt if nxt.startswith("com.myqops://") else str(httpx.URL(str(hop.url)).join(nxt))

    raise LoginBlocked("Too many redirects while completing sign-in")


def automatic_login(email: str, password: str, store: TokenStore) -> Tokens:
    """Full headless login. Raises InvalidCredentials / LoginBlocked."""
    if not email or not password:
        raise InvalidCredentials("Set MYQ_EMAIL and MYQ_PASSWORD in .env")
    print("Signing in to MyQ...")
    code, verifier = fetch_authorization_code(email, password)
    # Exchange immediately: MyQ expires authorization codes very quickly.
    payload = exchange_code(code, verifier)
    return _persist(payload, store)


# ------------------------------------------------------ interactive login ----


def extract_code(redirect_url: str) -> str:
    """Pull the authorization code out of a ``com.myqops://`` redirect."""
    code = parse_qs(urlsplit(redirect_url.strip()).query).get("code", [""])[0]
    if not code:
        raise ValueError("That URL has no ?code= parameter")
    return code


def complete_login(redirect_url: str, verifier: str, store: TokenStore) -> Tokens:
    payload = exchange_code(extract_code(redirect_url), verifier)
    return _persist(payload, store)


def open_browser(url: str) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", url], check=False)
        elif sys.platform.startswith("linux"):
            subprocess.run(["xdg-open", url], check=False)
        else:
            subprocess.run(["start", url], shell=True, check=False)
    except OSError:
        pass


def interactive_login(store: TokenStore) -> Tokens:
    """Browser-assisted fallback for MFA/SSO accounts.

    Each attempt builds a fresh authorization URL, because the PKCE verifier is
    bound to it and codes are single-use.
    """
    print()
    print("=" * 68)
    print("  MyQ browser login")
    print("=" * 68)
    print()
    print("Note: MyQ expires authorization codes within about a minute, so copy")
    print("the redirect URL promptly once you have signed in.")

    while True:
        auth_url, verifier = build_auth_url()

        print()
        print("Opening the MyQ sign-in page in your browser...")
        open_browser(auth_url)
        print()
        print("If it did not open, paste this into your browser:")
        print(f"  {auth_url}")
        print()
        print("  1. Sign in with your MyQ email and password.")
        print("  2. Clear the Cloudflare check if it appears.")
        print("  3. The browser will try to open 'com.myqops://android?code=...'")
        print("     and show an error. That error is expected and means it worked.")
        print("  4. Copy the FULL URL from the address bar and paste it below.")
        print()

        while True:
            redirect_url = input("Paste the com.myqops:// URL here: ").strip()
            if not redirect_url:
                continue
            try:
                return complete_login(redirect_url, verifier, store)
            except ValueError as exc:
                print(f"  {exc} — try again.")
            except MyQError as exc:
                print()
                print(f"  {exc.message}")
                print()
                answer = input("  Start over with a fresh login? [Y/n] ").strip().lower()
                if answer in ("", "y", "yes"):
                    break
                raise
