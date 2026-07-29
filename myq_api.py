"""
MyQ Garage Door API - Working Implementation
Uses Playwright browser login to bypass Cloudflare + automatic token refresh
"""

import json
import time
import gzip
import asyncio
import hashlib
import base64
import secrets
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass
import aiohttp
from urllib.parse import parse_qs, urlsplit, urlencode
from dotenv import load_dotenv

load_dotenv()


# Constants
API_BASE = "https://devices.myq-cloud.com"
GDO_API_BASE = "https://account-devices-gdo.myq-cloud.com"
OAUTH_BASE_URI = "https://partner-identity.myq-cloud.com"
OAUTH_AUTHORIZE_URI = "https://partner-identity.myq-cloud.com/connect/authorize"
OAUTH_TOKEN_URI = "https://partner-identity.myq-cloud.com/connect/token"

# Android credentials (from @hjdhjd/myq)
OAUTH_CLIENT_ID = "ANDROID_CGI_MYQ"
OAUTH_CLIENT_SECRET_B64 = "VUQ0RFhuS3lQV3EyNUJTdw=="
OAUTH_CLIENT_SECRET = base64.b64decode(OAUTH_CLIENT_SECRET_B64).decode()
OAUTH_REDIRECT_URI = "com.myqops://android"
MYQ_APP_ID = "D9D7B25035D549D8A3EA16A9FFB8C927D4A19B55B8944011B2670A8321BF8312"

API_USER_AGENT = "sdk_gphone_x86/Android 11"
APP_VERSION = "5.242.0.72704"

TOKENS_FILE = Path(__file__).parent / "myq_tokens.json"


def generate_pkce_pair():
    """Generate PKCE code verifier and challenge."""
    code_verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode().rstrip('=')
    return code_verifier, code_challenge


@dataclass
class DoorState:
    name: str
    serial_number: str
    state: str  # open, closed, opening, closing
    online: bool
    last_update: str
    last_status: str
    is_open: bool
    is_closed: bool


@dataclass
class TokenInfo:
    access_token: str
    refresh_token: str
    expires_at: float
    account_id: str
    device_serial: str
    cf_cookie: str = ""
    token_scope: str = "MyQ_Residential offline_access"


async def playwright_login(email: str, password: str) -> dict:
    """
    Login to MyQ using Playwright browser to bypass Cloudflare.
    Returns token dict on success, raises on failure.
    """
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

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

    print(f"[{datetime.now().isoformat()}] Launching browser for login...")

    headless = os.getenv("HEADLESS", "true").lower() in ("true", "1", "yes")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        stealth = Stealth()
        await stealth.apply_stealth_async(context)
        page = await context.new_page()

        auth_code = None

        # Intercept the app:// redirect to capture the auth code
        async def handle_route(route):
            nonlocal auth_code
            url = route.request.url
            if "com.myqops://" in url and "code=" in url:
                parsed = urlsplit(url)
                query = parse_qs(parsed.query)
                auth_code = query.get('code', [''])[0]
                print(f"[{datetime.now().isoformat()}] Captured auth code from redirect")
                await route.abort()
            else:
                await route.continue_()

        # Also watch for navigation to the app scheme
        page.on("close", lambda: None)

        try:
            # Navigate to auth URL
            print(f"[{datetime.now().isoformat()}] Loading login page...")
            resp = await page.goto(auth_url, wait_until="domcontentloaded", timeout=60000)

            # Handle Cloudflare challenge - try to click turnstile checkbox
            for attempt in range(15):
                content = await page.content()
                title = await page.title()
                if 'Just a moment' in content or 'challenge-platform' in content or 'Verify you are human' in content:
                    print(f"[{datetime.now().isoformat()}] Cloudflare challenge, attempting to solve... ({attempt + 1}/15)")
                    # Try to click the Turnstile checkbox via iframe
                    try:
                        frames = page.frames
                        for frame in frames:
                            try:
                                checkbox = await frame.query_selector('input[type="checkbox"]')
                                if checkbox:
                                    await checkbox.click()
                                    print(f"[{datetime.now().isoformat()}] Clicked Turnstile checkbox")
                                    await asyncio.sleep(3)
                                    break
                                # Try clicking the challenge body
                                body = await frame.query_selector('body')
                                if body and 'challenge' in (await frame.content()).lower():
                                    await body.click()
                                    await asyncio.sleep(3)
                            except Exception:
                                pass
                    except Exception:
                        pass
                    await asyncio.sleep(3)
                elif '429' in title:
                    print(f"[{datetime.now().isoformat()}] Rate limited, waiting... ({attempt + 1}/15)")
                    await asyncio.sleep(10)
                else:
                    break

            # Wait for the login form
            print(f"[{datetime.now().isoformat()}] Waiting for login form...")
            try:
                await page.wait_for_selector('input[name="Email"]', timeout=30000)
            except Exception:
                screenshot_path = Path(__file__).parent / "login_debug.png"
                await page.screenshot(path=str(screenshot_path))
                content = await page.content()
                if 'Just a moment' in content or 'challenge' in content.lower():
                    raise Exception(
                        "Cloudflare is blocking requests (rate limited). "
                        "Wait 5-10 minutes and try again, or use: python setup.py --proxy"
                    )
                raise Exception(f"Login form not found. Screenshot saved to {screenshot_path}")

            # Fill in credentials
            print(f"[{datetime.now().isoformat()}] Entering credentials...")
            await page.fill('input[name="Email"]', email)
            await page.fill('input[name="Password"]', password)

            # Set up interception for the app redirect before clicking
            await page.route("**/com.myqops://**", handle_route)

            # Click Sign In
            print(f"[{datetime.now().isoformat()}] Clicking Sign In...")
            # Try multiple selector strategies
            try:
                await page.click('button:has-text("Sign In")', timeout=5000)
            except Exception:
                try:
                    await page.click('input[type="submit"]', timeout=3000)
                except Exception:
                    await page.keyboard.press("Enter")

            # Wait for the redirect - the page will navigate to com.myqops://
            # which will either be caught by route interception or cause a navigation error
            print(f"[{datetime.now().isoformat()}] Waiting for OAuth redirect...")

            for _ in range(30):
                if auth_code:
                    break

                # Check for error messages on the page
                try:
                    error_el = await page.query_selector('.validation-summary-errors, .field-validation-error')
                    if error_el:
                        error_text = await error_el.inner_text()
                        if error_text.strip():
                            raise Exception(f"Login error: {error_text.strip()}")
                except Exception as e:
                    if "Login error:" in str(e):
                        raise

                # Check if URL contains the auth code (browser may show error page)
                current_url = page.url
                if "com.myqops://" in current_url and "code=" in current_url:
                    parsed = urlsplit(current_url)
                    query = parse_qs(parsed.query)
                    auth_code = query.get('code', [''])[0]
                    break

                # Check console for the redirect URL
                await asyncio.sleep(1)

            if not auth_code:
                # Last resort: check if there's a redirect we missed
                screenshot_path = Path(__file__).parent / "login_debug.png"
                await page.screenshot(path=str(screenshot_path))
                print(f"[{datetime.now().isoformat()}] Saved debug screenshot to {screenshot_path}")
                raise Exception("Failed to capture auth code - check login_debug.png")

        finally:
            await browser.close()

    # Exchange auth code for tokens using httpx (sync is fine here)
    print(f"[{datetime.now().isoformat()}] Exchanging code for tokens...")
    import httpx

    token_data = {
        'client_id': OAUTH_CLIENT_ID,
        'code': auth_code,
        'code_verifier': code_verifier,
        'grant_type': 'authorization_code',
        'redirect_uri': OAUTH_REDIRECT_URI,
        'scope': 'MyQ_Residential offline_access',
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            OAUTH_TOKEN_URI,
            data=token_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": API_USER_AGENT,
            },
        )
        if resp.status_code != 200:
            raise Exception(f"Token exchange failed: {resp.status_code} - {resp.text}")
        tokens = resp.json()

    result = {
        'access_token': tokens['access_token'],
        'refresh_token': tokens.get('refresh_token', ''),
        'expires_in': tokens.get('expires_in', 1800),
        'scope': tokens.get('scope', 'MyQ_Residential offline_access'),
        'code_verifier': code_verifier,
    }

    print(f"[{datetime.now().isoformat()}] Login successful!")
    return result


class MyQAPI:
    """MyQ API Client with Playwright login and automatic token refresh"""

    def __init__(self, tokens_file: Path = TOKENS_FILE):
        self.tokens_file = tokens_file
        self.tokens: Optional[TokenInfo] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        self._load_tokens()

        self._email = os.getenv("MYQ_EMAIL", "")
        self._password = os.getenv("MYQ_PASSWORD", "")

    def _load_tokens(self):
        """Load tokens from file"""
        if self.tokens_file.exists():
            try:
                data = json.loads(self.tokens_file.read_text())
                expires_at = data.get('expires_at', time.time() + 1800)
                self.tokens = TokenInfo(
                    access_token=data['access_token'],
                    refresh_token=data['refresh_token'],
                    expires_at=expires_at,
                    account_id=data.get('account_id', ''),
                    device_serial=data.get('device_serial', ''),
                    cf_cookie=data.get('cf_cookie', ''),
                    token_scope=data.get('token_scope', 'MyQ_Residential offline_access'),
                )
            except Exception as e:
                print(f"[{datetime.now().isoformat()}] Failed to load tokens: {e}")
                self.tokens = None

    def _save_tokens(self):
        """Save tokens to file"""
        if self.tokens:
            data = {
                'access_token': self.tokens.access_token,
                'refresh_token': self.tokens.refresh_token,
                'expires_at': self.tokens.expires_at,
                'account_id': self.tokens.account_id,
                'device_serial': self.tokens.device_serial,
                'expires_in': 1800,
                'token_type': 'Bearer',
                'scope': 'MyQ_Residential offline_access',
                'cf_cookie': self.tokens.cf_cookie,
                'token_scope': self.tokens.token_scope,
            }
            self.tokens_file.write_text(json.dumps(data, indent=2))

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session"""
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=True)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    def _get_api_headers(self, extra: dict = None) -> dict:
        """Generate headers for API calls"""
        headers = {
            "Accept-Encoding": "gzip",
            "App-Version": APP_VERSION,
            "BrandId": "1",
            "MyQApplicationId": MYQ_APP_ID,
            "User-Agent": API_USER_AGENT,
        }
        if self.tokens:
            headers["Authorization"] = f"Bearer {self.tokens.access_token}"
        if extra:
            headers.update(extra)
        return headers

    async def login(self, email: str = None, password: str = None) -> bool:
        """
        Login using Playwright browser to bypass Cloudflare.
        Falls back to token refresh if available.
        """
        email = email or self._email
        password = password or self._password

        if not email or not password:
            raise Exception("Email and password required. Set MYQ_EMAIL and MYQ_PASSWORD environment variables.")

        print(f"[{datetime.now().isoformat()}] Starting login for {email[:3]}***...")

        token_result = await playwright_login(email, password)

        access_token = token_result['access_token']
        refresh_token = token_result.get('refresh_token', '')
        scope = token_result.get('scope', 'MyQ_Residential offline_access')

        # Get account info
        print(f"[{datetime.now().isoformat()}] Getting account info...")
        session = await self._get_session()
        headers = self._get_api_headers({"Authorization": f"Bearer {access_token}"})

        account_id = ''
        device_serial = ''

        async with session.get(f"{API_BASE}/api/v6.2/Accounts", headers=headers) as resp:
            if resp.status == 200:
                data = await resp.read()
                encoding = resp.headers.get('Content-Encoding', '')
                if encoding == 'gzip':
                    data = gzip.decompress(data)
                elif encoding == 'br':
                    import brotli
                    data = brotli.decompress(data)
                accounts_data = json.loads(data.decode())
                accounts = accounts_data.get('items', [])
                if accounts:
                    account_id = accounts[0].get('id', '')
                    print(f"[{datetime.now().isoformat()}] Account: {account_id}")

        if account_id:
            async with session.get(
                f"{API_BASE}/api/v6.2/Accounts/{account_id}/Devices", headers=headers
            ) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    encoding = resp.headers.get('Content-Encoding', '')
                    if encoding == 'gzip':
                        data = gzip.decompress(data)
                    elif encoding == 'br':
                        import brotli
                        data = brotli.decompress(data)
                    devices_data = json.loads(data.decode())
                    for device in devices_data.get('items', []):
                        if device.get('device_family') == 'garagedoor':
                            device_serial = device.get('serial_number', '')
                            name = device.get('name', 'Unknown')
                            print(f"[{datetime.now().isoformat()}] Garage door: {name} ({device_serial})")
                            break

        self.tokens = TokenInfo(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=time.time() + token_result.get('expires_in', 1800) - 60,
            account_id=account_id,
            device_serial=device_serial,
            cf_cookie=self.tokens.cf_cookie if self.tokens else '',
            token_scope=scope,
        )
        self._save_tokens()

        print(f"[{datetime.now().isoformat()}] Login complete! Tokens saved.")
        return True

    async def _refresh_token(self) -> bool:
        """Refresh the access token using refresh_token"""
        if not self.tokens or not self.tokens.refresh_token:
            return False

        print(f"[{datetime.now().isoformat()}] Refreshing access token...")

        session = await self._get_session()

        refresh_data = {
            'client_id': OAUTH_CLIENT_ID,
            'client_secret': OAUTH_CLIENT_SECRET,
            'grant_type': 'refresh_token',
            'redirect_uri': OAUTH_REDIRECT_URI,
            'refresh_token': self.tokens.refresh_token,
            'scope': self.tokens.token_scope,
        }

        try:
            async with session.post(
                OAUTH_TOKEN_URI,
                data=refresh_data,
                headers=self._get_api_headers({
                    "Content-Type": "application/x-www-form-urlencoded",
                }),
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    self.tokens.access_token = result['access_token']
                    self.tokens.refresh_token = result.get('refresh_token', self.tokens.refresh_token)
                    self.tokens.expires_at = time.time() + result.get('expires_in', 1800) - 60
                    self.tokens.token_scope = result.get('scope', self.tokens.token_scope)
                    self._save_tokens()
                    print(f"[{datetime.now().isoformat()}] Token refreshed!")
                    return True
                else:
                    error = await resp.text()
                    print(f"[{datetime.now().isoformat()}] Token refresh failed: {resp.status} - {error}")
                    return False
        except Exception as e:
            print(f"[{datetime.now().isoformat()}] Token refresh error: {e}")
            return False

    async def _ensure_valid_token(self):
        """Ensure we have a valid access token, refresh or re-login if needed"""
        async with self._lock:
            if not self.tokens:
                # Try reloading from file (might have been captured by proxy)
                self._load_tokens()
                if not self.tokens:
                    raise Exception(
                        "No tokens available. Run: python login.py (interactive browser login) "
                        "or python setup.py --proxy (capture from phone app)"
                    )

            # Token expires within 5 minutes - refresh
            if time.time() > self.tokens.expires_at - 300:
                success = await self._refresh_token()
                if not success:
                    # Try reloading from file before giving up
                    self._load_tokens()
                    if self.tokens and time.time() < self.tokens.expires_at - 300:
                        return
                    raise Exception(
                        "Token refresh failed. Run: python login.py to get fresh tokens"
                    )

    async def _request(self, method: str, path: str, body: dict = None, use_gdo_host: bool = False) -> dict:
        """Make authenticated API request with auto-retry on 401"""
        await self._ensure_valid_token()

        session = await self._get_session()
        base_url = GDO_API_BASE if use_gdo_host else API_BASE
        url = f"{base_url}{path}"

        headers = self._get_api_headers({
            'Authorization': f'Bearer {self.tokens.access_token}',
        })

        if use_gdo_host and self.tokens.cf_cookie:
            headers['Cookie'] = self.tokens.cf_cookie

        kwargs = {'headers': headers}
        if body:
            kwargs['json'] = body

        async with session.request(method, url, **kwargs) as resp:
            data = await resp.read()

            encoding = resp.headers.get('Content-Encoding', '')
            try:
                if encoding == 'gzip':
                    data = gzip.decompress(data)
                elif encoding == 'br':
                    import brotli
                    data = brotli.decompress(data)
            except Exception:
                pass

            # Capture Cloudflare cookie
            set_cookie = resp.headers.get('Set-Cookie', '')
            if '__cf_bm=' in set_cookie:
                for cookie_part in set_cookie.split(';'):
                    if '__cf_bm=' in cookie_part:
                        self.tokens.cf_cookie = cookie_part.strip()
                        self._save_tokens()
                        break

            if resp.status == 401:
                print(f"[{datetime.now().isoformat()}] 401 - refreshing and retrying...")
                refreshed = await self._refresh_token()
                if not refreshed:
                    await self.login()
                headers['Authorization'] = f'Bearer {self.tokens.access_token}'
                kwargs['headers'] = headers
                async with session.request(method, url, **kwargs) as retry_resp:
                    data = await retry_resp.read()
                    if retry_resp.status >= 400:
                        raise Exception(f"API error {retry_resp.status} after retry")
                    if retry_resp.status == 202:
                        return {"status": "accepted", "code": 202}
                    if data:
                        text = data.decode() if isinstance(data, bytes) else data
                        return json.loads(text) if text else {}
                    return {}
            elif resp.status >= 400:
                raise Exception(f"API error {resp.status}: {data.decode() if isinstance(data, bytes) else data}")

            if resp.status == 202:
                return {"status": "accepted", "code": 202}

            if data:
                text = data.decode() if isinstance(data, bytes) else data
                return json.loads(text) if text else {}
            return {}

    async def get_devices(self) -> list[dict]:
        """Get all devices"""
        result = await self._request('GET', f'/api/v6.2/Accounts/{self.tokens.account_id}/Devices')
        return result.get('items', [])

    async def get_garage_door(self) -> dict:
        """Get garage door device"""
        devices = await self.get_devices()
        for device in devices:
            if device.get('device_family') == 'garagedoor':
                if self.tokens and device.get('serial_number'):
                    self.tokens.device_serial = device['serial_number']
                    self._save_tokens()
                return device
        raise Exception("No garage door found")

    async def get_door_state(self) -> DoorState:
        """Get current door state"""
        door = await self.get_garage_door()
        state = door.get('state', {})
        door_state = state.get('door_state', 'unknown')

        return DoorState(
            name=door.get('name', 'Unknown'),
            serial_number=door.get('serial_number', ''),
            state=door_state,
            online=state.get('online', False),
            last_update=state.get('last_update', ''),
            last_status=state.get('last_status', ''),
            is_open=door_state in ('open', 'opening'),
            is_closed=door_state == 'closed',
        )

    async def set_door_state(self, action: str) -> dict:
        """Set door state (open/close) using the GDO API"""
        door = await self.get_garage_door()
        serial = door['serial_number']

        result = await self._request(
            'PUT',
            f'/api/v6.0/Accounts/{self.tokens.account_id}/door_openers/{serial}/{action}',
            body=None,
            use_gdo_host=True
        )
        return result

    async def open_door(self) -> dict:
        """Open the garage door"""
        return await self.set_door_state('open')

    async def close_door(self) -> dict:
        """Close the garage door"""
        return await self.set_door_state('close')

    async def close(self):
        """Close the session"""
        if self._session and not self._session.closed:
            await self._session.close()


# Global API instance
_api: Optional[MyQAPI] = None


def get_api() -> MyQAPI:
    """Get or create API instance"""
    global _api
    if _api is None:
        _api = MyQAPI()
    return _api


async def reset_api():
    """Reset the API instance"""
    global _api
    if _api:
        await _api.close()
    _api = None
