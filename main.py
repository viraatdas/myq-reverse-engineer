"""MyQ Garage API - FastAPI Application.

This API provides endpoints to control your MyQ garage door
from Apple Shortcuts or any HTTP client.
"""

import time
from contextlib import asynccontextmanager
from typing import Optional
from collections import defaultdict

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config import get_settings, Settings
from myq_client import (
    get_myq_client, 
    MyQClient, 
    MyQAuthError, 
    MyQAPIError,
    DoorState,
    reset_client,
)


# Rate limiting storage
rate_limit_store: dict[str, list[float]] = defaultdict(list)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup - don't authenticate here to avoid rate limiting issues
    settings = get_settings()
    print(f"Starting MyQ Garage API...")
    print(f"Debug mode: {settings.debug}")
    print("Will authenticate on first request...")
    
    yield
    
    # Shutdown
    print("Shutting down MyQ Garage API...")


app = FastAPI(
    title="MyQ Garage API",
    description="Control your MyQ garage door from Apple Shortcuts",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware for web clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Response Models
class StatusResponse(BaseModel):
    """Garage door status response."""
    device_id: str
    name: str
    state: str
    is_open: bool
    is_closed: bool
    last_updated: Optional[str] = None


class ActionResponse(BaseModel):
    """Action response."""
    success: bool
    message: str
    device_id: str


class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    authenticated: bool


class DevicesResponse(BaseModel):
    """List of devices response."""
    devices: list[StatusResponse]
    count: int


# Dependencies
async def verify_api_key(x_api_key: str = Header(..., alias="X-API-Key")) -> str:
    """Verify the API key from request header."""
    settings = get_settings()
    if x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key


async def rate_limit(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """Simple rate limiting."""
    client_ip = request.client.host if request.client else "unknown"
    current_time = time.time()
    window_start = current_time - settings.rate_limit_window
    
    # Clean old entries
    rate_limit_store[client_ip] = [
        t for t in rate_limit_store[client_ip] if t > window_start
    ]
    
    # Check rate limit
    if len(rate_limit_store[client_ip]) >= settings.rate_limit_requests:
        raise HTTPException(
            status_code=429, 
            detail="Rate limit exceeded. Please wait before making more requests."
        )
    
    # Add current request
    rate_limit_store[client_ip].append(current_time)


async def get_client() -> MyQClient:
    """Get authenticated MyQ client."""
    client = get_myq_client()
    try:
        await client.ensure_authenticated()
    except MyQAuthError as e:
        raise HTTPException(status_code=503, detail=f"MyQ authentication failed: {e}")
    return client


# Exception handlers
@app.exception_handler(MyQAuthError)
async def myq_auth_error_handler(request: Request, exc: MyQAuthError):
    """Handle MyQ authentication errors."""
    return JSONResponse(
        status_code=503,
        content={"detail": f"MyQ authentication error: {str(exc)}"}
    )


@app.exception_handler(MyQAPIError)
async def myq_api_error_handler(request: Request, exc: MyQAPIError):
    """Handle MyQ API errors."""
    return JSONResponse(
        status_code=502,
        content={"detail": f"MyQ API error: {str(exc)}"}
    )


# Endpoints
@app.get("/health", response_model=HealthResponse, tags=["Health"])
async def health_check():
    """Health check endpoint (no authentication required)."""
    client = get_myq_client()
    return HealthResponse(
        status="healthy",
        authenticated=client.access_token is not None and time.time() < client.token_expiry
    )


@app.get(
    "/status",
    response_model=StatusResponse,
    tags=["Garage Door"],
    dependencies=[Depends(verify_api_key), Depends(rate_limit)],
)
async def get_status(
    device_id: Optional[str] = None,
    client: MyQClient = Depends(get_client),
):
    """Get the current status of your garage door.
    
    Returns the door state (open, closed, opening, closing, etc.)
    
    - **device_id**: Optional. If you have multiple doors, specify which one.
    """
    try:
        door = await client.get_door_state(device_id)
        return StatusResponse(
            device_id=door.device_id,
            name=door.name,
            state=door.state.value,
            is_open=door.state in (DoorState.OPEN, DoorState.OPENING),
            is_closed=door.state == DoorState.CLOSED,
            last_updated=door.last_updated,
        )
    except MyQAPIError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get(
    "/devices",
    response_model=DevicesResponse,
    tags=["Garage Door"],
    dependencies=[Depends(verify_api_key), Depends(rate_limit)],
)
async def list_devices(client: MyQClient = Depends(get_client)):
    """List all garage door devices.
    
    Useful if you have multiple garage doors and need to find their device IDs.
    """
    devices = await client.get_devices(force_refresh=True)
    return DevicesResponse(
        devices=[
            StatusResponse(
                device_id=d.device_id,
                name=d.name,
                state=d.state.value,
                is_open=d.state in (DoorState.OPEN, DoorState.OPENING),
                is_closed=d.state == DoorState.CLOSED,
                last_updated=d.last_updated,
            )
            for d in devices
        ],
        count=len(devices),
    )


@app.post(
    "/open",
    response_model=ActionResponse,
    tags=["Garage Door"],
    dependencies=[Depends(verify_api_key), Depends(rate_limit)],
)
async def open_door(
    device_id: Optional[str] = None,
    client: MyQClient = Depends(get_client),
):
    """Open the garage door.
    
    - **device_id**: Optional. If you have multiple doors, specify which one.
    """
    try:
        # Get current state first
        door = await client.get_door_state(device_id)
        
        if door.state == DoorState.OPEN:
            return ActionResponse(
                success=True,
                message="Door is already open",
                device_id=door.device_id,
            )
        
        if door.state == DoorState.OPENING:
            return ActionResponse(
                success=True,
                message="Door is already opening",
                device_id=door.device_id,
            )
        
        await client.open_door(door.device_id)
        return ActionResponse(
            success=True,
            message="Door opening command sent",
            device_id=door.device_id,
        )
    except MyQAPIError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post(
    "/close",
    response_model=ActionResponse,
    tags=["Garage Door"],
    dependencies=[Depends(verify_api_key), Depends(rate_limit)],
)
async def close_door(
    device_id: Optional[str] = None,
    client: MyQClient = Depends(get_client),
):
    """Close the garage door.
    
    - **device_id**: Optional. If you have multiple doors, specify which one.
    """
    try:
        # Get current state first
        door = await client.get_door_state(device_id)
        
        if door.state == DoorState.CLOSED:
            return ActionResponse(
                success=True,
                message="Door is already closed",
                device_id=door.device_id,
            )
        
        if door.state == DoorState.CLOSING:
            return ActionResponse(
                success=True,
                message="Door is already closing",
                device_id=door.device_id,
            )
        
        await client.close_door(door.device_id)
        return ActionResponse(
            success=True,
            message="Door closing command sent",
            device_id=door.device_id,
        )
    except MyQAPIError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post(
    "/toggle",
    response_model=ActionResponse,
    tags=["Garage Door"],
    dependencies=[Depends(verify_api_key), Depends(rate_limit)],
)
async def toggle_door(
    device_id: Optional[str] = None,
    client: MyQClient = Depends(get_client),
):
    """Toggle the garage door (open if closed, close if open).
    
    - **device_id**: Optional. If you have multiple doors, specify which one.
    """
    try:
        door = await client.get_door_state(device_id)
        
        if door.state in (DoorState.OPEN, DoorState.OPENING):
            await client.close_door(door.device_id)
            return ActionResponse(
                success=True,
                message="Door closing command sent",
                device_id=door.device_id,
            )
        else:
            await client.open_door(door.device_id)
            return ActionResponse(
                success=True,
                message="Door opening command sent",
                device_id=door.device_id,
            )
    except MyQAPIError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/reset-auth", tags=["Admin"], dependencies=[Depends(verify_api_key)])
async def reset_authentication():
    """Reset the MyQ authentication (force re-login).
    
    Use this if authentication becomes stale.
    """
    await reset_client()
    return {"message": "Authentication reset. Will re-authenticate on next request."}


if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
