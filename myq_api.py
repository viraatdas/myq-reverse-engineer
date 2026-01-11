"""
MyQ Garage Door API - Working Implementation
Uses OAuth login with automatic token refresh
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
from bs4 import BeautifulSoup


# Constants - Using Android credentials (more reliable for automated access)
API_BASE = "https://devices.myq-cloud.com"
GDO_API_BASE = "https://account-devices-gdo.myq-cloud.com"  # For door actions
OAUTH_BASE_URI = "https://partner-identity.myq-cloud.com"
OAUTH_AUTHORIZE_URI = "https://partner-identity.myq-cloud.com/connect/authorize"
OAUTH_TOKEN_URI = "https://partner-identity.myq-cloud.com/connect/token"

# Android credentials (from @hjdhjd/myq)
OAUTH_CLIENT_ID = "ANDROID_CGI_MYQ"
OAUTH_CLIENT_SECRET_B64 = "VUQ0RFhuS3lQV3EyNUJTdw=="
OAUTH_CLIENT_SECRET = base64.b64decode(OAUTH_CLIENT_SECRET_B64).decode()
OAUTH_REDIRECT_URI = "com.myqops://android"
MYQ_APP_ID = "D9D7B25035D549D8A3EA16A9FFB8C927D4A19B55B8944011B2670A8321BF8312"

# User agents
LOGIN_USER_AGENT = "Mozilla/5.0 (Linux; Android 11; sdk_gphone_x86) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/83.0.4103.106 Mobile Safari/537.36"
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


class MyQAPI:
    """MyQ API Client with OAuth login and automatic token refresh"""
    
    def __init__(self, tokens_file: Path = TOKENS_FILE, proxy: str = None):
        self.tokens_file = tokens_file
        self.tokens: Optional[TokenInfo] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        self._load_tokens()
        
        self._email = os.getenv("MYQ_EMAIL", "")
        self._password = os.getenv("MYQ_PASSWORD", "")
        
        # Optional proxy for bypassing rate limits
        # Format: "http://user:pass@host:port" or just "http://host:port"
        self._proxy = proxy or os.getenv("MYQ_PROXY", "")
    
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
            # Create session without default headers - we'll set them per request
            connector = aiohttp.TCPConnector(ssl=True)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session
    
    def _get_proxy(self) -> Optional[str]:
        """Get proxy URL if configured"""
        return self._proxy if self._proxy else None
    
    def _get_login_headers(self, extra: dict = None) -> dict:
        """Generate headers for OAuth login flow (browser-like)"""
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
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
    
    def _extract_cookies(self, response) -> str:
        """Extract cookies from response headers as a cookie string"""
        cookies = []
        for cookie in response.cookies.values():
            cookies.append(f"{cookie.key}={cookie.value}")
        return "; ".join(cookies)

    async def login(self, email: str = None, password: str = None, max_retries: int = 3) -> bool:
        """
        Perform full OAuth login using Android client credentials.
        Uses PKCE authorization code flow with browser-like headers.
        """
        email = email or self._email
        password = password or self._password
        
        if not email or not password:
            raise Exception("Email and password required. Set MYQ_EMAIL and MYQ_PASSWORD environment variables.")
        
        print(f"[{datetime.now().isoformat()}] Starting OAuth login for {email[:3]}***...")
        
        session = await self._get_session()
        
        # Generate PKCE pair
        code_verifier, code_challenge = generate_pkce_pair()
        
        try:
            # Step 1: Get authorization page with PKCE challenge
            print(f"[{datetime.now().isoformat()}] Step 1: Getting authorization page...")
            
            # Build auth URL with all required params (matching hjdhjd/myq)
            auth_params = {
                'acr_values': 'unified_flow:v1  brand:myq',  # Note: double space is intentional
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
            
            html = None
            login_url = None
            cookies = ""
            
            for attempt in range(max_retries):
                async with session.get(
                    auth_url,
                    headers=self._get_login_headers(),
                    allow_redirects=False,
                    proxy=self._get_proxy(),
                ) as resp:
                    if resp.status == 429:
                        wait_time = 60 * (attempt + 1)
                        print(f"[{datetime.now().isoformat()}] Rate limited (429), waiting {wait_time}s... (attempt {attempt + 1}/{max_retries})")
                        await asyncio.sleep(wait_time)
                        continue
                    elif resp.status not in (200, 302):
                        raise Exception(f"Failed to get auth page: {resp.status}")
                    
                    # Check if we got the expected cookies
                    if len(resp.cookies) < 3:
                        print(f"[{datetime.now().isoformat()}] Warning: Expected 3+ cookies, got {len(resp.cookies)}")
                    
                    # Follow redirect if 302
                    if resp.status == 302:
                        redirect_url = resp.headers.get('Location', '')
                        if redirect_url.startswith('/'):
                            redirect_url = f"{OAUTH_BASE_URI}{redirect_url}"
                        
                        cookies = self._extract_cookies(resp)
                        
                        async with session.get(
                            redirect_url,
                            headers=self._get_login_headers({"Cookie": cookies}),
                            allow_redirects=True,
                            proxy=self._get_proxy(),
                        ) as redirect_resp:
                            html = await redirect_resp.text()
                            login_url = str(redirect_resp.url)
                            cookies = self._extract_cookies(redirect_resp) or cookies
                    else:
                        html = await resp.text()
                        login_url = str(resp.url)
                        cookies = self._extract_cookies(resp)
                    break
            
            if html is None:
                raise Exception("Failed to get auth page after retries - rate limited by MyQ/Cloudflare")
            
            # Step 2: Parse login form
            print(f"[{datetime.now().isoformat()}] Step 2: Parsing login form...")
            soup = BeautifulSoup(html, 'html.parser')
            
            # Check for Cloudflare challenge
            if 'cf-browser-verification' in html or 'challenge-platform' in html:
                raise Exception("Cloudflare browser verification required - cannot automate login")
            
            # Find the verification token
            token_input = soup.find('input', {'name': '__RequestVerificationToken'})
            if not token_input:
                raise Exception("Login form verification token not found")
            
            verification_token = token_input.get('value', '')
            
            # Step 3: Submit login credentials
            print(f"[{datetime.now().isoformat()}] Step 3: Submitting login credentials...")
            
            login_data = {
                'Email': email,
                'Password': password,
                'UnifiedFlowRequested': 'True',
                '__RequestVerificationToken': verification_token,
                'brand': 'myq',
            }
            
            async with session.post(
                login_url,
                data=login_data,
                headers=self._get_login_headers({
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cookie": cookies,
                    "cache-control": "max-age=0",
                    "origin": "null",
                    "sec-fetch-site": "same-origin",
                    "sec-fetch-user": "?1",
                }),
                allow_redirects=False,
                proxy=self._get_proxy(),
            ) as resp:
                if resp.status not in (302, 303):
                    error_text = await resp.text()
                    if 'invalid' in error_text.lower() or 'incorrect' in error_text.lower():
                        raise Exception("Invalid email or password")
                    raise Exception(f"Login failed: {resp.status}")
                
                # Check for successful login (should have cookies)
                if len(resp.cookies) < 2:
                    raise Exception("Invalid myQ credentials - login did not return expected cookies")
                
                redirect_location = resp.headers.get('Location', '')
                login_cookies = self._extract_cookies(resp)
            
            # Step 4: Follow redirect to get authorization code
            print(f"[{datetime.now().isoformat()}] Step 4: Following redirect to get auth code...")
            
            if redirect_location.startswith('/'):
                redirect_url = f"{OAUTH_BASE_URI}{redirect_location}"
            else:
                redirect_url = redirect_location
            
            async with session.get(
                redirect_url,
                headers=self._get_login_headers({
                    "Cookie": login_cookies,
                    "cache-control": "max-age=0",
                    "origin": "null",
                    "sec-fetch-site": "same-origin",
                    "sec-fetch-user": "?1",
                }),
                allow_redirects=False,
                proxy=self._get_proxy(),
            ) as resp:
                final_redirect = resp.headers.get('Location', '')
            
            # Parse the authorization code from the redirect URL
            parsed = urlsplit(final_redirect)
            query_params = parse_qs(parsed.query)
            
            auth_code = query_params.get('code', [''])[0]
            scope = query_params.get('scope', ['MyQ_Residential offline_access'])[0]
            
            if not auth_code:
                raise Exception(f"No authorization code in redirect: {final_redirect[:100]}")
            
            print(f"[{datetime.now().isoformat()}] Got authorization code: {auth_code[:20]}...")
            
            # Step 5: Exchange code for tokens
            print(f"[{datetime.now().isoformat()}] Step 5: Exchanging code for tokens...")
            
            token_data = {
                'client_id': OAUTH_CLIENT_ID,
                'code': auth_code,
                'code_verifier': code_verifier,
                'grant_type': 'authorization_code',
                'redirect_uri': OAUTH_REDIRECT_URI,
                'scope': 'MyQ_Residential offline_access',
            }
            
            async with session.post(
                OAUTH_TOKEN_URI,
                data=token_data,
                headers=self._get_api_headers({
                    "Content-Type": "application/x-www-form-urlencoded",
                }),
                proxy=self._get_proxy(),
            ) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    raise Exception(f"Token exchange failed: {resp.status} - {error}")
                
                token_response = await resp.json()
            
            access_token = token_response['access_token']
            refresh_token = token_response.get('refresh_token', '')
            
            # Step 6: Get account info
            print(f"[{datetime.now().isoformat()}] Step 6: Getting account info...")
            
            async with session.get(
                f"{API_BASE}/api/v6.2/Accounts",
                headers=self._get_api_headers({"Authorization": f"Bearer {access_token}"}),
            ) as resp:
                if resp.status != 200:
                    raise Exception(f"Failed to get accounts: {resp.status}")
                
                data = await resp.read()
                encoding = resp.headers.get('Content-Encoding', '')
                if encoding == 'gzip':
                    data = gzip.decompress(data)
                elif encoding == 'br':
                    import brotli
                    data = brotli.decompress(data)
                
                accounts_data = json.loads(data.decode())
            
            accounts = accounts_data.get('items', [])
            if not accounts:
                raise Exception("No accounts found")
            
            account_id = accounts[0].get('id', '')
            print(f"[{datetime.now().isoformat()}] Found account: {account_id}")
            
            # Step 7: Get device serial
            print(f"[{datetime.now().isoformat()}] Step 7: Getting devices...")
            
            async with session.get(
                f"{API_BASE}/api/v6.2/Accounts/{account_id}/Devices",
                headers=self._get_api_headers({"Authorization": f"Bearer {access_token}"}),
            ) as resp:
                data = await resp.read()
                encoding = resp.headers.get('Content-Encoding', '')
                if encoding == 'gzip':
                    data = gzip.decompress(data)
                elif encoding == 'br':
                    import brotli
                    data = brotli.decompress(data)
                
                devices_data = json.loads(data.decode())
            
            device_serial = ''
            for device in devices_data.get('items', []):
                if device.get('device_family') == 'garagedoor':
                    device_serial = device.get('serial_number', '')
                    print(f"[{datetime.now().isoformat()}] Found garage door: {device_serial}")
                    break
            
            # Save tokens
            self.tokens = TokenInfo(
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=time.time() + token_response.get('expires_in', 1800) - 60,
                account_id=account_id,
                device_serial=device_serial,
                cf_cookie=self.tokens.cf_cookie if self.tokens else '',
                token_scope=scope,
            )
            self._save_tokens()
            
            print(f"[{datetime.now().isoformat()}] ✅ Login successful! Tokens saved.")
            return True
            
        except Exception as e:
            print(f"[{datetime.now().isoformat()}] ❌ Login failed: {e}")
            raise
    
    async def _refresh_token(self) -> bool:
        """Refresh the access token using refresh_token"""
        if not self.tokens or not self.tokens.refresh_token:
            return False
        
        print(f"[{datetime.now().isoformat()}] Refreshing access token...")
        
        session = await self._get_session()
        
        # Use the decoded client secret for refresh (per hjdhjd/myq)
        refresh_data = {
            'client_id': OAUTH_CLIENT_ID,
            'client_secret': OAUTH_CLIENT_SECRET,  # Decoded from base64
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
                    "Authorization": "Bearer old-token",
                    "isRefresh": "true",
                }),
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    self.tokens.access_token = result['access_token']
                    self.tokens.refresh_token = result.get('refresh_token', self.tokens.refresh_token)
                    self.tokens.expires_at = time.time() + result.get('expires_in', 1800) - 60
                    self.tokens.token_scope = result.get('scope', self.tokens.token_scope)
                    self._save_tokens()
                    print(f"[{datetime.now().isoformat()}] Token refreshed successfully!")
                    return True
                else:
                    error = await resp.text()
                    print(f"[{datetime.now().isoformat()}] Token refresh failed: {resp.status} - {error}")
                    return False
        except Exception as e:
            print(f"[{datetime.now().isoformat()}] Token refresh error: {e}")
            return False
    
    async def _ensure_valid_token(self):
        """Ensure we have a valid access token, login if needed"""
        async with self._lock:
            # No tokens at all - try to login
            if not self.tokens:
                print(f"[{datetime.now().isoformat()}] No tokens found, attempting login...")
                try:
                    await self.login()
                except Exception as e:
                    raise Exception(
                        f"No tokens available and login failed: {e}\n"
                        "Please capture fresh tokens from the myQ app using a proxy like Proxyman or Charles."
                    )
                return
            
            # Token expires within 5 minutes - try refresh first, then login
            if time.time() > self.tokens.expires_at - 300:
                success = await self._refresh_token()
                if not success:
                    print(f"[{datetime.now().isoformat()}] Refresh failed, attempting full login...")
                    try:
                        await self.login()
                    except Exception as e:
                        raise Exception(
                            f"Token refresh failed and re-login failed: {e}\n"
                            "MyQ/Cloudflare may be blocking automated logins. "
                            "Please capture fresh tokens from the myQ app."
                        )
    
    async def _request(self, method: str, path: str, body: dict = None, use_gdo_host: bool = False) -> dict:
        """Make authenticated API request"""
        await self._ensure_valid_token()
        
        session = await self._get_session()
        base_url = GDO_API_BASE if use_gdo_host else API_BASE
        url = f"{base_url}{path}"
        
        headers = self._get_api_headers({
            'Authorization': f'Bearer {self.tokens.access_token}',
        })
        
        # Add Cloudflare cookie if available (needed for GDO endpoints)
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
            
            # Check for new Cloudflare cookie in response
            set_cookie = resp.headers.get('Set-Cookie', '')
            if '__cf_bm=' in set_cookie:
                for cookie_part in set_cookie.split(';'):
                    if '__cf_bm=' in cookie_part:
                        self.tokens.cf_cookie = cookie_part.strip()
                        self._save_tokens()
                        break
            
            # Handle 401 - token might be invalid, try re-login
            if resp.status == 401:
                print(f"[{datetime.now().isoformat()}] Got 401, attempting re-login...")
                await self.login()
                headers['Authorization'] = f'Bearer {self.tokens.access_token}'
                async with session.request(method, url, **kwargs) as retry_resp:
                    data = await retry_resp.read()
                    if retry_resp.status >= 400:
                        raise Exception(f"API error {retry_resp.status} after re-login: {data.decode() if isinstance(data, bytes) else data}")
                    resp = retry_resp
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
