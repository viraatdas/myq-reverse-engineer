"""Patch for pymyq library to fix token exchange bug.

The pymyq library has a bug where it sends the authorization code 
as the scope value instead of the actual scope.

This module patches the library to fix this issue.
"""

import asyncio
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from aiohttp import ClientSession
from bs4 import BeautifulSoup
from pkce import generate_code_verifier, get_code_challenge

from pymyq.const import (
    OAUTH_AUTHORIZE_URI,
    OAUTH_BASE_URI,
    OAUTH_CLIENT_ID,
    OAUTH_CLIENT_SECRET,
    OAUTH_REDIRECT_URI,
    OAUTH_TOKEN_URI,
)
from pymyq.errors import AuthenticationError, InvalidCredentialsError
from pymyq.api import API


async def patched_oauth_authenticate(self) -> Tuple[str, int]:
    """Patched OAuth authentication that fixes the scope bug."""
    
    async with ClientSession() as session:
        # Retrieve authentication page
        print("DEBUG: Retrieving authentication page...")
        resp, html = await self.request(
            method="get",
            returns="text",
            url=OAUTH_AUTHORIZE_URI,
            websession=session,
            headers={"redirect": "follow"},
            params={
                "client_id": OAUTH_CLIENT_ID,
                "code_challenge": get_code_challenge(self._code_verifier),
                "code_challenge_method": "S256",
                "redirect_uri": OAUTH_REDIRECT_URI,
                "response_type": "code",
                "scope": "MyQ_Residential offline_access",
            },
            login_request=True,
        )

        # Parse login form
        print("DEBUG: Scanning login page for fields...")
        soup = BeautifulSoup(html, "html.parser")
        
        forms = soup.find_all("form")
        data = {}
        for form in forms:
            have_email = False
            have_password = False
            have_submit = False
            for field in form.find_all("input"):
                if field.get("type"):
                    if field.get("type").lower() == "hidden":
                        data.update({
                            field.get("name", "NONAME"): field.get("value", "NOVALUE")
                        })
                    elif field.get("type").lower() == "email":
                        data.update({field.get("name", "Email"): self.username})
                        have_email = True
                    elif field.get("type").lower() == "password":
                        data.update({
                            field.get("name", "Password"): self._API__credentials.get("password")
                        })
                        have_password = True
                    elif field.get("type").lower() == "submit":
                        have_submit = True
                        
            if have_email and have_password and have_submit:
                break
            data = {}

        if len(data) == 0:
            raise AuthenticationError("Form with required fields not found")

        # Submit login
        print("DEBUG: Performing login to MyQ...")
        resp, _ = await self.request(
            method="post",
            returns="response",
            url=resp.url,
            websession=session,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": resp.cookies.output(attrs=[]),
            },
            data=data,
            allow_redirects=False,
            login_request=True,
        )

        # Check cookies for successful login
        if len(resp.cookies) < 2:
            self._invalid_credentials = True
            raise InvalidCredentialsError("Invalid MyQ credentials provided")

        # Follow redirect to get auth code
        print("DEBUG: Calling redirect page...")
        resp, _ = await self.request(
            method="get",
            returns="response",
            url=f"{OAUTH_BASE_URI}{resp.headers['Location']}",
            websession=session,
            headers={"Cookie": resp.cookies.output(attrs=[])},
            allow_redirects=False,
            login_request=True,
        )

        # Get authorization code from redirect URL
        redirect_url = resp.headers['Location']
        print(f"DEBUG: Redirect URL: {redirect_url[:100]}...")
        
        parsed = urlsplit(redirect_url)
        query_params = parse_qs(parsed.query)
        
        auth_code = query_params.get("code", [""])[0]
        # FIX: Get the actual scope from the redirect, not the code!
        scope = query_params.get("scope", ["MyQ_Residential offline_access"])[0]
        
        print(f"DEBUG: Got auth code: {auth_code[:30]}...")
        print(f"DEBUG: Got scope: {scope}")

        # Exchange code for token - THIS IS THE FIXED VERSION
        print("DEBUG: Getting token...")
        resp, data = await self.request(
            returns="json",
            method="post",
            url=OAUTH_TOKEN_URI,
            websession=session,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_id": OAUTH_CLIENT_ID,
                "client_secret": OAUTH_CLIENT_SECRET,
                "code": auth_code,
                "code_verifier": self._code_verifier,
                "grant_type": "authorization_code",
                "redirect_uri": OAUTH_REDIRECT_URI,
                "scope": scope,  # FIXED: Use actual scope, not code
            },
            login_request=True,
        )

        if not isinstance(data, dict):
            raise AuthenticationError(f"Unexpected response type: {type(data)}")

        token = f"{data.get('token_type')} {data.get('access_token')}"
        try:
            expires = int(data.get("expires_in"))
        except (TypeError, ValueError):
            expires = 3600

        return token, expires


def apply_patch():
    """Apply the patch to pymyq.api.API."""
    API._oauth_authenticate = patched_oauth_authenticate
    print("pymyq patch applied!")

