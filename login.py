#!/usr/bin/env python3
"""
MyQ Interactive Login

Opens the MyQ OAuth page in your real browser. You log in normally,
and when the redirect happens, paste the URL back here.

This bypasses Cloudflare because your real browser handles the challenge.

Usage:
    python login.py
"""

import json
import hashlib
import base64
import secrets
import subprocess
import sys
import time
import os
from pathlib import Path
from urllib.parse import urlencode, urlsplit, parse_qs
from dotenv import load_dotenv

load_dotenv()

OAUTH_BASE_URI = "https://partner-identity.myq-cloud.com"
OAUTH_AUTHORIZE_URI = "https://partner-identity.myq-cloud.com/connect/authorize"
OAUTH_TOKEN_URI = "https://partner-identity.myq-cloud.com/connect/token"
OAUTH_CLIENT_ID = "ANDROID_CGI_MYQ"
OAUTH_REDIRECT_URI = "com.myqops://android"
MYQ_APP_ID = "D9D7B25035D549D8A3EA16A9FFB8C927D4A19B55B8944011B2670A8321BF8312"
API_USER_AGENT = "sdk_gphone_x86/Android 11"
APP_VERSION = "5.242.0.72704"
TOKENS_FILE = Path(__file__).parent / "myq_tokens.json"


def generate_pkce_pair():
    code_verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode().rstrip('=')
    return code_verifier, code_challenge


def main():
    print()
    print("=" * 60)
    print("  MyQ Interactive Login")
    print("=" * 60)
    print()

    code_verifier, code_challenge = generate_pkce_pair()

    auth_params = {
        'acr_values': 'unified_flow:v1  brand:myq',
        'client_id': OAUTH_CLIENT_ID,
        'code_challenge': code_challenge,
        'code_challenge_method': 'S256',
        'prompt': 'login',
        'ui_locales': 'en-US',
        'redirect_uri': OAUTH_REDIRECT_URI,
        'response_type': 'code',
        'scope': 'MyQ_Residential offline_access',
    }
    auth_url = f"{OAUTH_AUTHORIZE_URI}?{urlencode(auth_params)}"

    print("Opening MyQ login in your browser...")
    print()

    # Open in system browser
    if sys.platform == "darwin":
        subprocess.run(["open", auth_url])
    elif sys.platform == "linux":
        subprocess.run(["xdg-open", auth_url])
    else:
        subprocess.run(["start", auth_url], shell=True)

    print("1. Log in with your MyQ credentials in the browser")
    print("2. Complete the Cloudflare verification if prompted")
    print("3. After login, the browser will try to redirect to")
    print('   "com.myqops://android?code=..." and FAIL')
    print()
    print("4. Copy the FULL URL from the browser's address bar")
    print("   (it starts with com.myqops://android?code=...)")
    print()

    while True:
        redirect_url = input("Paste the redirect URL here: ").strip()

        if not redirect_url:
            continue

        # Parse the auth code
        if "code=" in redirect_url:
            parsed = urlsplit(redirect_url)
            query = parse_qs(parsed.query)
            auth_code = query.get('code', [''])[0]
            scope = query.get('scope', ['MyQ_Residential offline_access'])[0]

            if auth_code:
                print(f"\nGot auth code: {auth_code[:30]}...")
                break

        print("Could not find auth code in that URL. Try again.")

    # Exchange code for tokens
    print("Exchanging code for tokens...")

    import httpx
    token_data = {
        'client_id': OAUTH_CLIENT_ID,
        'code': auth_code,
        'code_verifier': code_verifier,
        'grant_type': 'authorization_code',
        'redirect_uri': OAUTH_REDIRECT_URI,
        'scope': 'MyQ_Residential offline_access',
    }

    with httpx.Client() as client:
        resp = client.post(
            OAUTH_TOKEN_URI,
            data=token_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": API_USER_AGENT,
            },
        )
        if resp.status_code != 200:
            print(f"Token exchange failed: {resp.status_code} - {resp.text}")
            sys.exit(1)
        tokens = resp.json()

    access_token = tokens['access_token']
    print("Got access token!")

    # Get account info
    print("Getting account info...")
    headers = {
        'Authorization': f'Bearer {access_token}',
        'MyQApplicationId': MYQ_APP_ID,
        'User-Agent': API_USER_AGENT,
        'App-Version': APP_VERSION,
        'BrandId': '1',
    }

    account_id = ''
    device_serial = ''

    with httpx.Client() as client:
        acct_resp = client.get(
            "https://devices.myq-cloud.com/api/v6.2/Accounts",
            headers=headers,
        )
        if acct_resp.status_code == 200:
            accounts = acct_resp.json().get('items', [])
            if accounts:
                account_id = accounts[0].get('id', '')
                print(f"Account: {account_id}")

                dev_resp = client.get(
                    f"https://devices.myq-cloud.com/api/v6.2/Accounts/{account_id}/Devices",
                    headers=headers,
                )
                if dev_resp.status_code == 200:
                    for d in dev_resp.json().get('items', []):
                        if d.get('device_family') == 'garagedoor':
                            device_serial = d.get('serial_number', '')
                            name = d.get('name', 'Unknown')
                            print(f"Garage door: {name} (serial: {device_serial})")
                            break

    # Save tokens
    token_file = {
        'access_token': access_token,
        'refresh_token': tokens.get('refresh_token', ''),
        'expires_at': int(time.time()) + tokens.get('expires_in', 1800),
        'account_id': account_id,
        'device_serial': device_serial,
        'expires_in': tokens.get('expires_in', 1800),
        'token_type': 'Bearer',
        'scope': tokens.get('scope', 'MyQ_Residential offline_access'),
        'token_scope': tokens.get('scope', 'MyQ_Residential offline_access'),
        'cf_cookie': '',
    }
    TOKENS_FILE.write_text(json.dumps(token_file, indent=2))

    print()
    print("=" * 60)
    print("  Login successful! Tokens saved.")
    print("=" * 60)
    print()
    print("Start the API server with:")
    print("  uv run python server.py")
    print()
    print("Or test with:")
    print("  uv run python setup.py --test")
    print()


if __name__ == "__main__":
    main()
