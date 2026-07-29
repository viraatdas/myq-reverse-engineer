"""One-time interactive MyQ login.

MyQ fronts its identity provider with Cloudflare, which reliably blocks
headless automation. Rather than fight it, this flow hands the OAuth page to a
real browser, lets a human clear any challenge, and collects the authorization
code from the redirect that the browser cannot follow (``com.myqops://``).

This module is intentionally **not** part of the Lambda bundle — it only ever
runs on a workstation.
"""

from __future__ import annotations

import base64
import hashlib
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
    OAUTH_REDIRECT_URI,
    OAUTH_TOKEN_URI,
)
from .tokens import TokenStore, Tokens

SCOPE = "MyQ_Residential offline_access"


def generate_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
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


def extract_code(redirect_url: str) -> str:
    """Pull the authorization code out of the ``com.myqops://`` redirect."""
    query = parse_qs(urlsplit(redirect_url.strip()).query)
    code = query.get("code", [""])[0]
    if not code:
        raise ValueError("That URL has no ?code= parameter")
    return code


def exchange_code(code: str, verifier: str) -> dict:
    """Trade the authorization code for an access + refresh token pair."""
    resp = httpx.post(
        OAUTH_TOKEN_URI,
        data={
            "client_id": OAUTH_CLIENT_ID,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": OAUTH_REDIRECT_URI,
            "scope": SCOPE,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": API_USER_AGENT,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Token exchange failed: {resp.status_code} {resp.text[:300]}")
    return resp.json()


def resolve_account(access_token: str) -> tuple[str, str, str]:
    """Look up ``(account_id, device_serial, door_name)`` for a new token."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "MyQApplicationId": MYQ_APP_ID,
        "User-Agent": API_USER_AGENT,
        "App-Version": APP_VERSION,
        "BrandId": "1",
    }
    with httpx.Client(headers=headers, timeout=30) as client:
        resp = client.get(f"{API_BASE}/api/v6.2/Accounts")
        resp.raise_for_status()
        accounts = resp.json().get("items", [])
        if not accounts:
            raise RuntimeError("This MyQ login has no accounts")
        account_id = accounts[0].get("id", "")

        resp = client.get(f"{API_BASE}/api/v6.2/Accounts/{account_id}/Devices")
        resp.raise_for_status()
        for device in resp.json().get("items", []):
            if device.get("device_family") == "garagedoor":
                return account_id, device.get("serial_number", ""), device.get("name", "Garage Door")

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


def complete_login(redirect_url: str, verifier: str, store: TokenStore) -> Tokens:
    """Finish a login given the redirect URL the browser landed on."""
    code = extract_code(redirect_url)
    payload = exchange_code(code, verifier)
    account_id, serial, name = resolve_account(payload["access_token"])
    tokens = tokens_from_response(payload, account_id, serial)
    store.save(tokens)
    if name:
        print(f"Found garage door: {name} ({serial})")
    return tokens


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
    """Guide a human through the OAuth flow and persist the result."""
    auth_url, verifier = build_auth_url()

    print()
    print("=" * 68)
    print("  MyQ login")
    print("=" * 68)
    print()
    print("Opening the MyQ sign-in page in your browser...")
    open_browser(auth_url)
    print()
    print("If it did not open, paste this into your browser:")
    print(f"  {auth_url}")
    print()
    print("  1. Sign in with your MyQ email and password.")
    print("  2. Clear the Cloudflare check if it appears.")
    print("  3. The browser will then try to open 'com.myqops://android?code=...'")
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
