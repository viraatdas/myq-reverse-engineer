"""FastAPI application.

Design notes for the endpoints:

* Every door command is exposed on **both GET and POST**. iOS Shortcuts'
  "Get Contents of URL" defaults to GET, and location/Bluetooth automations are
  far easier to build when the whole action is just a URL.
* ``?wait=true`` polls until the door actually reaches the target state, so a
  shortcut can report what happened rather than what was requested.
* ``?format=text`` returns a plain sentence instead of JSON.
* Auth accepts a header *or* a query parameter, because a URL-only shortcut
  cannot set headers.
"""

from __future__ import annotations

import logging
import secrets
import time
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from . import __version__
from .client import MyQClient
from .config import Settings, get_settings
from .errors import DoorOffline, InvalidApiKey, MyQError, NotConfigured, RateLimited
from .models import ActionResult, DeviceList, DoorList, DoorStatus, ErrorBody, Health, utcnow
from .tokens import build_token_store

log = logging.getLogger(__name__)

Format = Literal["json", "text"]


# ---------------- lifecycle ----------------


def ensure_state(application: FastAPI) -> None:
    """Build settings + client once, idempotently.

    Called from the lifespan handler locally and lazily on Lambda. Lambda runs
    with ``lifespan="off"``: Mangum enters a lifespan context *per invocation*,
    so relying on startup events there would rebuild the HTTP session and drop
    every cache on each request.
    """
    if getattr(application.state, "client", None) is not None:
        return
    settings = get_settings()
    store = build_token_store(settings)
    application.state.settings = settings
    application.state.client = MyQClient(store, settings)
    log.info(
        "MyQ API %s ready (tokens=%s, api_key=%s)",
        __version__,
        store.location,
        "configured" if settings.api_key else "MISSING",
    )
    if not settings.api_key:
        log.warning("API_KEY is not set — all protected endpoints will return 503")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_state(app)
    yield
    await app.state.client.close()


app = FastAPI(
    title="MyQ Garage Door API",
    description=(
        "Control a MyQ garage door over HTTP. Built for iOS Shortcuts: every "
        "command works over GET, supports plain-text responses, and can wait "
        "for the door to actually finish moving."
    ),
    version=__version__,
    lifespan=lifespan,
)


def get_client(request: Request) -> MyQClient:
    ensure_state(request.app)
    return request.app.state.client


def settings_of(request: Request) -> Settings:
    ensure_state(request.app)
    return request.app.state.settings


app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ---------------- auth & rate limiting ----------------

# Per-container, per-IP. Not a distributed limiter — it exists to blunt
# brute-force attempts against the API key, not to meter legitimate traffic.
_rate_state: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def rate_limit(request: Request) -> None:
    settings = settings_of(request)
    now = time.time()
    window_start = now - settings.rate_limit_window
    ip = _client_ip(request)

    hits = [t for t in _rate_state.get(ip, []) if t > window_start]
    if len(hits) >= settings.rate_limit_requests:
        raise RateLimited()
    hits.append(now)
    _rate_state[ip] = hits

    # Keep the table from growing without bound on a long-lived container.
    if len(_rate_state) > 1024:
        for key in [k for k, v in _rate_state.items() if not v or v[-1] < window_start]:
            _rate_state.pop(key, None)


async def require_api_key(
    request: Request,
    key: str | None = Query(
        None,
        description="API key, for clients that cannot set headers (iOS Shortcuts URLs)",
    ),
) -> None:
    """Authenticate via ``X-API-Key``, ``Authorization: Bearer``, or ``?key=``.

    Fails closed: if no API key is configured the endpoint is unavailable
    rather than open. An internet-reachable URL that opens a garage door must
    never default to unauthenticated.
    """
    settings = settings_of(request)
    if not settings.api_key:
        raise NotConfigured(
            "API_KEY is not set on the server, so protected endpoints are disabled"
        )

    presented = request.headers.get("x-api-key") or ""
    if not presented:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            presented = auth[7:].strip()
    if not presented and key:
        presented = key

    if not presented or not secrets.compare_digest(presented, settings.api_key):
        raise InvalidApiKey()


Protected = [Depends(rate_limit), Depends(require_api_key)]


# ---------------- error handling ----------------


@app.exception_handler(MyQError)
async def myq_error_handler(request: Request, exc: MyQError):
    if exc.detail:
        log.warning("%s: %s", exc.message, exc.detail)
    headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
    return JSONResponse(
        status_code=exc.status,
        content=jsonable_encoder(ErrorBody(error=exc.message)),
        headers=headers,
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    # Log the real cause, return an opaque message. The previous version
    # returned str(exc), leaking upstream internals to any caller.
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content=jsonable_encoder(ErrorBody(error="Internal server error")),
    )


def render(model, fmt: Format, status: int = 200):
    if fmt == "text":
        return PlainTextResponse(model.as_text() + "\n", status_code=status)
    return JSONResponse(status_code=status, content=jsonable_encoder(model))


# ---------------- public endpoints ----------------


@app.get("/", tags=["Info"], summary="API index")
async def root():
    return {
        "name": "MyQ Garage Door API",
        "version": __version__,
        "docs": "/docs",
        "endpoints": {
            "GET /health": "Health check (no auth)",
            "GET /status": "Full door status",
            "GET /state": "Bare door state as plain text (open/closed/...)",
            "GET|POST /open": "Open the door",
            "GET|POST /close": "Close the door",
            "GET|POST /toggle": "Toggle the door",
            "GET /doors": "All garage doors and their states",
            "GET /devices": "All MyQ devices (raw)",
            "POST /admin/refresh": "Force a token refresh",
        },
        "query_parameters": {
            "key": "API key, when you cannot set the X-API-Key header",
            "format": "json (default) or text",
            "wait": "true to poll until the door finishes moving",
            "timeout": "seconds to wait when wait=true",
            "serial": "target a specific door on multi-door accounts",
        },
        "timestamp": utcnow(),
    }


@app.get("/health", tags=["Info"], summary="Health check", response_model=Health)
async def health(request: Request, format: Format = "json"):
    """Unauthenticated liveness probe. Reports config state, never secrets."""
    settings = settings_of(request)
    client: MyQClient = get_client(request)

    tokens = client.tokens
    expires_in = int(tokens.expires_at - time.time()) if tokens else None

    if not settings.api_key:
        status = "unconfigured"
    elif tokens is None:
        status = "degraded"
    else:
        status = "ok"

    return render(
        Health(
            status=status,
            authenticated=tokens is not None,
            api_key_configured=bool(settings.api_key),
            token_store=client.store.location.split(":")[0],
            token_expires_in=expires_in,
            version=__version__,
        ),
        format,
    )


# ---------------- door endpoints ----------------


@app.get(
    "/status",
    tags=["Garage Door"],
    dependencies=Protected,
    summary="Current door status",
    response_model=None,
    responses={200: {"model": DoorStatus}},
)
async def status(
    request: Request,
    format: Format = "json",
    serial: str | None = Query(None, description="Target a specific door"),
):
    client: MyQClient = get_client(request)
    state = await client.get_door_state(serial, force=True)
    return render(DoorStatus.from_state(state), format)


@app.get(
    "/state",
    tags=["Garage Door"],
    dependencies=Protected,
    summary="Bare door state as plain text",
    response_class=PlainTextResponse,
)
async def state(
    request: Request,
    serial: str | None = Query(None, description="Target a specific door"),
):
    """Returns just ``open``/``closed``/``opening``/``closing``.

    Purpose-built for a Shortcuts ``If`` comparison — no JSON parsing needed.
    """
    client: MyQClient = get_client(request)
    door = await client.get_door_state(serial, force=True)
    return PlainTextResponse(door.state + "\n")


async def _command(
    request: Request,
    action: str,
    fmt: Format,
    wait: bool,
    timeout: float | None,
    serial: str | None,
    force: bool,
) -> JSONResponse | PlainTextResponse:
    client: MyQClient = get_client(request)
    target = "open" if action == "open" else "closed"
    already = ("open", "opening") if action == "open" else ("closed", "closing")

    before = await client.get_door_state(serial, force=True)

    # Idempotent by default: re-issuing "close" on a closed door is a no-op
    # rather than a wasted command. Shortcuts automations fire more than once.
    if before.state in already and not force:
        result = ActionResult(
            success=True,
            action=action,
            name=before.name,
            serial_number=before.serial_number,
            previous_state=before.state,
            state=before.state,
            confirmed=True,
            message=f"{before.name} is already {before.state}",
        )
        return render(result, fmt)

    if not before.online:
        # Commanding an offline opener silently does nothing upstream.
        raise DoorOffline(f"{before.name} is offline")

    await client.send_command(action, serial)

    if wait:
        final = await client.wait_for_state(target, serial, timeout)
        result = ActionResult(
            success=final.state == target,
            action=action,
            name=final.name,
            serial_number=final.serial_number,
            previous_state=before.state,
            state=final.state,
            confirmed=True,
            message=f"{final.name} is {final.state}",
        )
        return render(result, fmt)

    result = ActionResult(
        success=True,
        action=action,
        name=before.name,
        serial_number=before.serial_number,
        previous_state=before.state,
        state="opening" if action == "open" else "closing",
        confirmed=False,
        message=f"{before.name} is {'opening' if action == 'open' else 'closing'}",
    )
    return render(result, fmt)


def register_command(path: str, endpoint, summary: str) -> None:
    """Expose a command on GET and POST as two distinct OpenAPI operations.

    A single multi-method route would emit both verbs under one operation id,
    which makes the generated schema invalid.
    """
    for verb in ("GET", "POST"):
        app.add_api_route(
            path,
            endpoint,
            methods=[verb],
            summary=summary,
            operation_id=f"{endpoint.__name__}_{verb.lower()}",
            tags=["Garage Door"],
            dependencies=Protected,
            response_model=None,
            responses={200: {"model": ActionResult}},
        )


async def open_door(
    request: Request,
    format: Format = "json",
    wait: bool = Query(False, description="Poll until the door is fully open"),
    timeout: float | None = Query(None, description="Seconds to wait when wait=true"),
    serial: str | None = Query(None),
    force: bool = Query(False, description="Send the command even if already open"),
):
    return await _command(request, "open", format, wait, timeout, serial, force)


async def close_door(
    request: Request,
    format: Format = "json",
    wait: bool = Query(False, description="Poll until the door is fully closed"),
    timeout: float | None = Query(None, description="Seconds to wait when wait=true"),
    serial: str | None = Query(None),
    force: bool = Query(False, description="Send the command even if already closed"),
):
    return await _command(request, "close", format, wait, timeout, serial, force)


async def toggle_door(
    request: Request,
    format: Format = "json",
    wait: bool = Query(False, description="Poll until the door finishes moving"),
    timeout: float | None = Query(None, description="Seconds to wait when wait=true"),
    serial: str | None = Query(None),
):
    client: MyQClient = get_client(request)
    current = await client.get_door_state(serial, force=True)
    action = "close" if current.is_open else "open"
    # force=True: the state check above already decided, and a toggle should
    # never short-circuit into "already in that state".
    return await _command(request, action, format, wait, timeout, serial, True)


register_command("/open", open_door, "Open the door")
register_command("/close", close_door, "Close the door")
register_command("/toggle", toggle_door, "Toggle the door")


@app.get(
    "/doors",
    tags=["Garage Door"],
    dependencies=Protected,
    summary="List garage doors",
    response_model=None,
    responses={200: {"model": DoorList}},
)
async def doors(request: Request, format: Format = "json"):
    client: MyQClient = get_client(request)
    states = await client.list_doors()
    return render(
        DoorList(count=len(states), doors=[DoorStatus.from_state(s) for s in states]), format
    )


@app.get(
    "/devices",
    tags=["Devices"],
    dependencies=Protected,
    summary="List all MyQ devices",
    response_model=None,
    responses={200: {"model": DeviceList}},
)
async def devices(request: Request, format: Format = "json"):
    client: MyQClient = get_client(request)
    items = await client.get_devices(force=True)
    return render(DeviceList(count=len(items), devices=items), format)


# ---------------- admin ----------------


@app.post("/admin/refresh", tags=["Admin"], dependencies=Protected, summary="Force token refresh")
async def admin_refresh(request: Request):
    client: MyQClient = get_client(request)
    if client.tokens is None:
        raise NotConfigured("No tokens are stored")
    await client.refresh()
    return {
        "success": True,
        "message": "Token refreshed",
        "expires_in": int(client.tokens.expires_at - time.time()),
        "timestamp": utcnow(),
    }


@app.post("/admin/reset", tags=["Admin"], dependencies=Protected, summary="Reset cached state")
async def admin_reset(request: Request):
    """Drop cached devices and re-read tokens from the store."""
    client: MyQClient = get_client(request)
    await client.close()
    request.app.state.client = MyQClient(build_token_store(settings_of(request)), settings_of(request))
    return {"success": True, "message": "Client reset", "timestamp": utcnow()}
