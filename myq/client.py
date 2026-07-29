"""Async MyQ API client.

Talks to the endpoints used by the MyQ Android app. Notable differences from
the first implementation:

* Decompression is left to aiohttp instead of hand-rolled gzip/brotli calls
  that silently swallowed errors.
* Token refresh is guarded by a lock, so a burst of concurrent requests
  performs one refresh rather than N competing ones.
* Failures raise typed errors (see ``myq.errors``) instead of bare Exception.
* Devices are cached briefly — a command used to cost two upstream calls.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass

import aiohttp

from .config import Settings
from .errors import CommandTimeout, DeviceNotFound, ReauthRequired, UpstreamError
from .tokens import TokenStore, Tokens

log = logging.getLogger(__name__)

API_BASE = "https://devices.myq-cloud.com"
GDO_API_BASE = "https://account-devices-gdo.myq-cloud.com"
OAUTH_TOKEN_URI = "https://partner-identity.myq-cloud.com/connect/token"
OAUTH_AUTHORIZE_URI = "https://partner-identity.myq-cloud.com/connect/authorize"

# Credentials extracted from the MyQ Android app.
OAUTH_CLIENT_ID = "ANDROID_CGI_MYQ"
OAUTH_CLIENT_SECRET = base64.b64decode("VUQ0RFhuS3lQV3EyNUJTdw==").decode()
OAUTH_REDIRECT_URI = "com.myqops://android"
MYQ_APP_ID = "D9D7B25035D549D8A3EA16A9FFB8C927D4A19B55B8944011B2670A8321BF8312"
API_USER_AGENT = "sdk_gphone_x86/Android 11"
APP_VERSION = "5.242.0.72704"

OPEN_STATES = ("open", "opening")
CLOSED_STATES = ("closed", "closing")
# States that mean "the door is settled", i.e. polling can stop.
TERMINAL_STATES = ("open", "closed")


@dataclass
class DoorState:
    name: str
    serial_number: str
    state: str
    online: bool
    last_update: str
    last_status: str

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    @property
    def is_closed(self) -> bool:
        return self.state in CLOSED_STATES

    @property
    def is_moving(self) -> bool:
        return self.state in ("opening", "closing")


class MyQClient:
    """Authenticated client for one MyQ account."""

    def __init__(self, store: TokenStore, settings: Settings):
        self.store = store
        self.settings = settings
        self._tokens: Tokens | None = None
        self._session: aiohttp.ClientSession | None = None
        self._refresh_lock = asyncio.Lock()
        self._session_loop: asyncio.AbstractEventLoop | None = None
        self._devices: list[dict] = []
        self._devices_at: float = 0.0

    # ---------- plumbing ----------

    async def _get_session(self) -> aiohttp.ClientSession:
        # A ClientSession is bound to the loop that created it. On Lambda the
        # client outlives a single invocation, so if the loop is ever replaced
        # the old session is unusable — detect that and rebuild rather than
        # failing with "Event loop is closed".
        loop = asyncio.get_running_loop()
        if self._session is None or self._session.closed or self._session_loop is not loop:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20),
                # aiohttp transparently decompresses gzip responses.
                auto_decompress=True,
            )
            self._session_loop = loop
        return self._session

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {
            "Accept-Encoding": "gzip",
            "App-Version": APP_VERSION,
            "BrandId": "1",
            "MyQApplicationId": MYQ_APP_ID,
            "User-Agent": API_USER_AGENT,
        }
        if self._tokens:
            headers["Authorization"] = f"Bearer {self._tokens.access_token}"
        if extra:
            headers.update(extra)
        return headers

    @property
    def tokens(self) -> Tokens | None:
        if self._tokens is None:
            self._tokens = self.store.load()
        return self._tokens

    def _persist(self) -> None:
        if self._tokens:
            self.store.save(self._tokens)

    # ---------- auth ----------

    async def _ensure_token(self) -> None:
        """Guarantee a non-expired access token, refreshing if needed."""
        if self.tokens is None:
            raise ReauthRequired("No MyQ tokens are stored")

        if not self._tokens.is_expired(self.settings.token_refresh_skew):
            return

        async with self._refresh_lock:
            # Another coroutine may have refreshed while we waited for the lock.
            if not self._tokens.is_expired(self.settings.token_refresh_skew):
                return
            await self._refresh()

    async def refresh(self) -> None:
        """Force a token refresh, serialised against any in-flight refresh."""
        async with self._refresh_lock:
            await self._refresh()

    async def _refresh(self) -> None:
        """Exchange the refresh token for a new access token."""
        if not self._tokens or not self._tokens.refresh_token:
            raise ReauthRequired("No refresh token is stored")

        session = await self._get_session()
        payload = {
            "client_id": OAUTH_CLIENT_ID,
            "client_secret": OAUTH_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "redirect_uri": OAUTH_REDIRECT_URI,
            "refresh_token": self._tokens.refresh_token,
            "scope": self._tokens.scope,
        }
        try:
            async with session.post(
                OAUTH_TOKEN_URI,
                data=payload,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": API_USER_AGENT,
                },
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    # 400/401 here means the refresh token itself is dead.
                    if resp.status in (400, 401):
                        raise ReauthRequired(detail=f"refresh rejected: {resp.status} {body[:200]}")
                    raise UpstreamError(
                        "Could not refresh MyQ token",
                        detail=f"{resp.status} {body[:200]}",
                    )
                data = json.loads(body)
        except aiohttp.ClientError as exc:
            raise UpstreamError("Could not reach MyQ to refresh token", detail=str(exc)) from exc

        self._tokens.access_token = data["access_token"]
        self._tokens.refresh_token = data.get("refresh_token", self._tokens.refresh_token)
        self._tokens.expires_at = time.time() + float(data.get("expires_in", 1800))
        self._tokens.scope = data.get("scope", self._tokens.scope)
        self._persist()
        log.info("Refreshed MyQ access token: %s", self._tokens.redacted())

    # ---------- requests ----------

    def _capture_cf_cookie(self, resp: aiohttp.ClientResponse) -> None:
        """Persist the Cloudflare cookie MyQ's command host expects back."""
        for raw in resp.headers.getall("Set-Cookie", []):
            if "__cf_bm=" not in raw:
                continue
            cookie = raw.split(";", 1)[0].strip()
            if self._tokens and cookie != self._tokens.cf_cookie:
                self._tokens.cf_cookie = cookie
                self._persist()
            break

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        gdo_host: bool = False,
        _retried: bool = False,
    ) -> dict:
        await self._ensure_token()

        session = await self._get_session()
        url = f"{GDO_API_BASE if gdo_host else API_BASE}{path}"

        extra = {}
        if gdo_host and self._tokens.cf_cookie:
            extra["Cookie"] = self._tokens.cf_cookie
        kwargs: dict = {"headers": self._headers(extra)}
        if body is not None:
            kwargs["json"] = body

        try:
            async with session.request(method, url, **kwargs) as resp:
                raw = await resp.read()
                self._capture_cf_cookie(resp)

                if resp.status == 401 and not _retried:
                    # Token was revoked early. Refresh once, then replay.
                    log.info("MyQ returned 401; refreshing and retrying %s %s", method, path)
                    async with self._refresh_lock:
                        await self._refresh()
                    return await self._request(
                        method, path, body=body, gdo_host=gdo_host, _retried=True
                    )

                if resp.status in (401, 403):
                    raise ReauthRequired(detail=f"{resp.status} on {path}")
                if resp.status == 429:
                    raise UpstreamError(
                        "MyQ is rate limiting this account; try again shortly",
                        detail=f"429 on {path}",
                    )
                if resp.status >= 400:
                    raise UpstreamError(
                        f"MyQ returned an error ({resp.status})",
                        detail=f"{resp.status} on {path}: {raw[:200]!r}",
                    )

                # Commands return 202 Accepted with an empty body.
                if resp.status == 202 or not raw:
                    return {}
                try:
                    return json.loads(raw.decode())
                except (ValueError, UnicodeDecodeError) as exc:
                    raise UpstreamError(
                        "MyQ returned a malformed response", detail=str(exc)
                    ) from exc
        except asyncio.TimeoutError as exc:
            raise UpstreamError("MyQ request timed out", detail=str(exc)) from exc
        except aiohttp.ClientError as exc:
            raise UpstreamError("Could not reach MyQ", detail=str(exc)) from exc

    # ---------- devices ----------

    async def _ensure_account(self) -> str:
        """Resolve and cache the account id, discovering it if absent."""
        # Use the property, not the raw attribute: on a cold client the tokens
        # have not been read from the store yet, and skipping the lazy load
        # here costs a redundant /Accounts round trip on every first request.
        tokens = self.tokens
        if tokens and tokens.account_id:
            return tokens.account_id

        data = await self._request("GET", "/api/v6.2/Accounts")
        items = data.get("items", [])
        if not items:
            raise DeviceNotFound("This MyQ login has no accounts attached")
        self._tokens.account_id = items[0].get("id", "")
        self._persist()
        return self._tokens.account_id

    async def get_devices(self, *, force: bool = False) -> list[dict]:
        """All devices on the account, cached for ``device_cache_ttl`` seconds."""
        fresh = (time.time() - self._devices_at) < self.settings.device_cache_ttl
        if self._devices and fresh and not force:
            return self._devices

        account_id = await self._ensure_account()
        data = await self._request("GET", f"/api/v6.2/Accounts/{account_id}/Devices")
        self._devices = data.get("items", [])
        self._devices_at = time.time()
        return self._devices

    async def get_door(self, serial: str | None = None, *, force: bool = False) -> dict:
        """Find one garage door.

        Without ``serial`` this prefers the door recorded in the token store,
        then falls back to the first garage door on the account — so accounts
        with several doors behave predictably instead of picking at random.
        """
        devices = await self.get_devices(force=force)
        doors = [d for d in devices if d.get("device_family") == "garagedoor"]
        if not doors:
            raise DeviceNotFound()

        wanted = serial or (self._tokens.device_serial if self._tokens else "")
        if wanted:
            for door in doors:
                if door.get("serial_number") == wanted:
                    return door
            if serial:
                # An explicitly requested serial that does not exist is an error,
                # not something to silently paper over with a different door.
                raise DeviceNotFound(f"No garage door with serial {serial}")

        door = doors[0]
        if self._tokens and door.get("serial_number") != self._tokens.device_serial:
            self._tokens.device_serial = door.get("serial_number", "")
            self._persist()
        return door

    async def get_door_state(self, serial: str | None = None, *, force: bool = False) -> DoorState:
        door = await self.get_door(serial, force=force)
        state = door.get("state", {}) or {}
        return DoorState(
            name=door.get("name") or "Garage Door",
            serial_number=door.get("serial_number", ""),
            state=state.get("door_state", "unknown"),
            online=bool(state.get("online", False)),
            last_update=state.get("last_update", ""),
            last_status=state.get("last_status", ""),
        )

    async def list_doors(self) -> list[DoorState]:
        devices = await self.get_devices()
        doors = []
        for device in devices:
            if device.get("device_family") != "garagedoor":
                continue
            state = device.get("state", {}) or {}
            doors.append(
                DoorState(
                    name=device.get("name") or "Garage Door",
                    serial_number=device.get("serial_number", ""),
                    state=state.get("door_state", "unknown"),
                    online=bool(state.get("online", False)),
                    last_update=state.get("last_update", ""),
                    last_status=state.get("last_status", ""),
                )
            )
        return doors

    # ---------- commands ----------

    async def send_command(self, action: str, serial: str | None = None) -> DoorState:
        """Send open/close and return the state observed just before sending."""
        if action not in ("open", "close"):
            raise ValueError(f"unsupported action: {action}")

        before = await self.get_door_state(serial)
        account_id = await self._ensure_account()
        await self._request(
            "PUT",
            f"/api/v6.0/Accounts/{account_id}/door_openers/{before.serial_number}/{action}",
            gdo_host=True,
        )
        # The door is now moving, so the cached device list is stale.
        self._devices_at = 0.0
        return before

    async def wait_for_state(
        self,
        target: str,
        serial: str | None = None,
        timeout: float | None = None,
    ) -> DoorState:
        """Poll until the door reaches ``target`` (or stops moving).

        This is what makes an iOS Shortcut able to say "the garage is closed"
        truthfully rather than "a close command was sent".
        """
        timeout = timeout if timeout is not None else self.settings.command_wait_timeout
        deadline = time.monotonic() + timeout
        state = await self.get_door_state(serial, force=True)

        while time.monotonic() < deadline:
            if state.state == target:
                return state
            # Settled on the wrong terminal state — e.g. a safety sensor
            # reversed the door. Return immediately rather than burn the clock.
            if state.state in TERMINAL_STATES and not state.is_moving:
                if state.state != target:
                    remaining = deadline - time.monotonic()
                    # Give MyQ a moment to report movement before giving up.
                    if remaining < timeout - self.settings.command_poll_interval * 2:
                        return state
            await asyncio.sleep(self.settings.command_poll_interval)
            state = await self.get_door_state(serial, force=True)

        if state.state == target:
            return state
        raise CommandTimeout(
            f"Door is '{state.state}' after {timeout:.0f}s, expected '{target}'"
        )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
