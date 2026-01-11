"""
MyQ Garage Door API Server
FastAPI server for controlling your garage door
"""

import os
import time
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from myq_api import get_api, reset_api, MyQAPI, DoorState


# Configuration
API_KEY = os.getenv("API_KEY", "your-secret-api-key")
RATE_LIMIT_REQUESTS = 10
RATE_LIMIT_WINDOW = 60  # seconds

# Rate limiting storage
rate_limit_store: dict[str, list[float]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler"""
    print(f"🚗 Starting MyQ Garage Door API Server...")
    print(f"   API Key configured: {'Yes' if API_KEY != 'your-secret-api-key' else 'No (using default)'}")
    yield
    print("Shutting down...")
    await reset_api()


app = FastAPI(
    title="MyQ Garage Door API",
    description="Control your MyQ garage door with a simple REST API",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============== Models ==============

class StatusResponse(BaseModel):
    """Door status response"""
    name: str
    serial_number: str
    state: str
    is_open: bool
    is_closed: bool
    online: bool
    last_update: str
    last_status: str
    timestamp: str


class ActionResponse(BaseModel):
    """Action response"""
    success: bool
    message: str
    action: str
    door_name: str
    previous_state: str
    timestamp: str


class HealthResponse(BaseModel):
    """Health check response"""
    status: str
    api_connected: bool
    timestamp: str


class DevicesResponse(BaseModel):
    """All devices response"""
    count: int
    devices: list[dict]
    timestamp: str


class ErrorResponse(BaseModel):
    """Error response"""
    error: str
    detail: str
    timestamp: str


# ============== Dependencies ==============

async def verify_api_key(x_api_key: str = Header(None, alias="X-API-Key")):
    """Verify API key"""
    if API_KEY != "your-secret-api-key" and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return x_api_key


async def rate_limit(request: Request):
    """Simple rate limiting"""
    client_ip = request.client.host if request.client else "unknown"
    current_time = time.time()
    window_start = current_time - RATE_LIMIT_WINDOW
    
    # Clean old entries
    if client_ip in rate_limit_store:
        rate_limit_store[client_ip] = [
            t for t in rate_limit_store[client_ip] if t > window_start
        ]
    else:
        rate_limit_store[client_ip] = []
    
    # Check rate limit
    if len(rate_limit_store[client_ip]) >= RATE_LIMIT_REQUESTS:
        raise HTTPException(
            status_code=429, 
            detail=f"Rate limit exceeded. Max {RATE_LIMIT_REQUESTS} requests per {RATE_LIMIT_WINDOW} seconds."
        )
    
    rate_limit_store[client_ip].append(current_time)


def get_timestamp() -> str:
    """Get current timestamp"""
    return datetime.now(timezone.utc).isoformat()


# ============== Exception Handlers ==============

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle general exceptions"""
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal Server Error",
            "detail": str(exc),
            "timestamp": get_timestamp(),
        }
    )


# ============== Endpoints ==============

@app.get("/", tags=["Info"])
async def root():
    """API information"""
    return {
        "name": "MyQ Garage Door API",
        "version": "2.0.0",
        "endpoints": {
            "status": "GET /status - Get door status",
            "open": "POST /open - Open the door",
            "close": "POST /close - Close the door",
            "toggle": "POST /toggle - Toggle the door",
            "devices": "GET /devices - List all devices",
            "health": "GET /health - Health check",
        },
        "timestamp": get_timestamp(),
    }


@app.get("/health", response_model=HealthResponse, tags=["Health"])
async def health_check():
    """Health check endpoint"""
    api = get_api()
    connected = api.tokens is not None
    
    return HealthResponse(
        status="healthy" if connected else "degraded",
        api_connected=connected,
        timestamp=get_timestamp(),
    )


@app.get(
    "/status",
    response_model=StatusResponse,
    tags=["Garage Door"],
    dependencies=[Depends(verify_api_key), Depends(rate_limit)],
)
async def get_status():
    """
    Get current garage door status.
    
    Returns the current state of your garage door including:
    - Door state (open, closed, opening, closing)
    - Online status
    - Last update timestamp
    """
    try:
        api = get_api()
        state = await api.get_door_state()
        
        return StatusResponse(
            name=state.name,
            serial_number=state.serial_number,
            state=state.state,
            is_open=state.is_open,
            is_closed=state.is_closed,
            online=state.online,
            last_update=state.last_update,
            last_status=state.last_status,
            timestamp=get_timestamp(),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post(
    "/open",
    response_model=ActionResponse,
    tags=["Garage Door"],
    dependencies=[Depends(verify_api_key), Depends(rate_limit)],
)
async def open_door():
    """
    Open the garage door.
    
    Sends an open command to your garage door.
    Note: If door is already open or opening, returns success without action.
    """
    try:
        api = get_api()
        state = await api.get_door_state()
        
        if state.state in ('open', 'opening'):
            return ActionResponse(
                success=True,
                message=f"Door is already {state.state}",
                action="open",
                door_name=state.name,
                previous_state=state.state,
                timestamp=get_timestamp(),
            )
        
        await api.open_door()
        
        return ActionResponse(
            success=True,
            message="Door opening command sent",
            action="open",
            door_name=state.name,
            previous_state=state.state,
            timestamp=get_timestamp(),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post(
    "/close",
    response_model=ActionResponse,
    tags=["Garage Door"],
    dependencies=[Depends(verify_api_key), Depends(rate_limit)],
)
async def close_door():
    """
    Close the garage door.
    
    Sends a close command to your garage door.
    Note: If door is already closed or closing, returns success without action.
    """
    try:
        api = get_api()
        state = await api.get_door_state()
        
        if state.state in ('closed', 'closing'):
            return ActionResponse(
                success=True,
                message=f"Door is already {state.state}",
                action="close",
                door_name=state.name,
                previous_state=state.state,
                timestamp=get_timestamp(),
            )
        
        await api.close_door()
        
        return ActionResponse(
            success=True,
            message="Door closing command sent",
            action="close",
            door_name=state.name,
            previous_state=state.state,
            timestamp=get_timestamp(),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post(
    "/toggle",
    response_model=ActionResponse,
    tags=["Garage Door"],
    dependencies=[Depends(verify_api_key), Depends(rate_limit)],
)
async def toggle_door():
    """
    Toggle the garage door.
    
    Opens the door if closed, closes if open.
    """
    try:
        api = get_api()
        state = await api.get_door_state()
        
        if state.is_open:
            await api.close_door()
            action = "close"
            message = "Door closing command sent"
        else:
            await api.open_door()
            action = "open"
            message = "Door opening command sent"
        
        return ActionResponse(
            success=True,
            message=message,
            action=action,
            door_name=state.name,
            previous_state=state.state,
            timestamp=get_timestamp(),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get(
    "/devices",
    response_model=DevicesResponse,
    tags=["Devices"],
    dependencies=[Depends(verify_api_key), Depends(rate_limit)],
)
async def list_devices():
    """
    List all MyQ devices.
    
    Returns all devices associated with your MyQ account.
    """
    try:
        api = get_api()
        devices = await api.get_devices()
        
        return DevicesResponse(
            count=len(devices),
            devices=devices,
            timestamp=get_timestamp(),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post(
    "/refresh-token",
    tags=["Admin"],
    dependencies=[Depends(verify_api_key)],
)
async def refresh_token():
    """
    Force refresh the API token.
    
    Use this if you're experiencing authentication issues.
    """
    try:
        api = get_api()
        success = await api._refresh_token()
        
        if success:
            return {"success": True, "message": "Token refreshed successfully", "timestamp": get_timestamp()}
        else:
            raise HTTPException(status_code=500, detail="Failed to refresh token")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post(
    "/reset",
    tags=["Admin"],
    dependencies=[Depends(verify_api_key)],
)
async def reset_connection():
    """
    Reset the API connection.
    
    Use this to force a fresh connection to the MyQ API.
    """
    await reset_api()
    return {"success": True, "message": "API connection reset", "timestamp": get_timestamp()}


# ============== Run Server ==============

if __name__ == "__main__":
    import uvicorn
    
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")
    
    print(f"Starting server on {host}:{port}")
    uvicorn.run("server:app", host=host, port=port, reload=True)

