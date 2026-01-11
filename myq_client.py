"""MyQ API Client - Using the pymyq library with patches.

This client wraps the pymyq library for easier use with FastAPI.
Includes a patch to fix a bug in pymyq's token exchange.
"""

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# Apply patch before importing pymyq functions that use it
from myq_patch import apply_patch
apply_patch()

import pymyq
from pymyq.errors import AuthenticationError, InvalidCredentialsError, MyQError

from config import get_settings


class DoorState(str, Enum):
    """Garage door states."""
    OPEN = "open"
    CLOSED = "closed"
    OPENING = "opening"
    CLOSING = "closing"
    STOPPED = "stopped"
    UNKNOWN = "unknown"


@dataclass
class GarageDoor:
    """Represents a garage door device."""
    device_id: str
    name: str
    state: DoorState
    last_updated: Optional[str] = None


class MyQAuthError(Exception):
    """Authentication error."""
    pass


class MyQAPIError(Exception):
    """API error."""
    pass


class MyQClient:
    """Client for interacting with the MyQ API using pymyq library."""
    
    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self._api: Optional[pymyq.api.API] = None
        self._lock = asyncio.Lock()
        self._selected_account_id: Optional[str] = None
    
    @property
    def access_token(self) -> Optional[str]:
        """Check if we have an active token."""
        if self._api and self._api._security_token[0]:
            return self._api._security_token[0]
        return None
    
    @property
    def token_expiry(self) -> float:
        """Get token expiry time."""
        if self._api and self._api._security_token[1]:
            return self._api._security_token[1].timestamp()
        return 0
    
    async def authenticate(self) -> None:
        """Authenticate with MyQ using pymyq."""
        async with self._lock:
            try:
                print(f"Authenticating with MyQ using email: {self.email[:3]}***")
                self._api = await pymyq.login(self.email, self.password)
                print("Authentication successful!")
                
                # List all available accounts/homes
                print(f"Found {len(self._api.accounts)} account(s):")
                for account_id, account in self._api.accounts.items():
                    account_name = account.account_json.get("name", "Unknown")
                    print(f"  - {account_name} (ID: {account_id})")
                
                # Try to find "Viraat's Home" account
                target_home = "Viraat's Home"
                selected_account = None
                for account_id, account in self._api.accounts.items():
                    account_name = account.account_json.get("name", "")
                    if target_home.lower() in account_name.lower():
                        selected_account = account
                        print(f"Selected account: {account_name}")
                        break
                
                if selected_account:
                    # Filter to only show devices from this account
                    self._selected_account_id = selected_account.account_json.get("id")
                else:
                    print(f"Warning: Could not find '{target_home}', using first account")
                    self._selected_account_id = None
                
                print(f"Found {len(self._api.covers)} total garage door(s)")
                
            except InvalidCredentialsError as e:
                print(f"Invalid credentials error: {e}")
                raise MyQAuthError(
                    f"Invalid credentials. Please verify:\n"
                    f"1. You can log into the MyQ app with this email/password\n"
                    f"2. Your account doesn't have 2FA enabled\n"
                    f"Original error: {e}"
                )
            except AuthenticationError as e:
                print(f"Authentication error: {e}")
                raise MyQAuthError(
                    f"Authentication failed. MyQ may be blocking API access. "
                    f"Try logging into the MyQ app to verify your account is active. "
                    f"Original error: {e}"
                )
            except MyQError as e:
                print(f"MyQ error: {e}")
                raise MyQAuthError(f"MyQ error during auth: {e}")
            except Exception as e:
                print(f"Unexpected error: {type(e).__name__}: {e}")
                raise MyQAuthError(f"Unexpected error: {e}")
    
    async def ensure_authenticated(self) -> None:
        """Ensure we have a valid API connection."""
        if self._api is None:
            await self.authenticate()
        else:
            # Let pymyq handle token refresh
            try:
                await self._api._refresh_token()
            except Exception:
                # If refresh fails, re-authenticate
                await self.authenticate()
    
    async def get_devices(self, force_refresh: bool = False) -> list[GarageDoor]:
        """Get all garage door devices."""
        await self.ensure_authenticated()
        
        if force_refresh:
            await self._api.update_device_info()
        
        devices = []
        
        # If we have a selected account, only get devices from that account
        if self._selected_account_id and self._selected_account_id in self._api.accounts:
            account = self._api.accounts[self._selected_account_id]
            covers_to_check = account.covers
        else:
            # Otherwise get all covers
            covers_to_check = self._api.covers
        
        for device_id, device in covers_to_check.items():
            # Map pymyq state to our state enum
            device_state = device.device_json.get("state", {})
            door_state_str = device_state.get("door_state", "unknown").lower()
            
            try:
                state = DoorState(door_state_str)
            except ValueError:
                state = DoorState.UNKNOWN
            
            devices.append(GarageDoor(
                device_id=device_id,
                name=device.name,
                state=state,
                last_updated=device_state.get("last_update"),
            ))
        
        return devices
    
    async def get_door_state(self, device_id: Optional[str] = None) -> GarageDoor:
        """Get the state of a specific door or the first door found."""
        devices = await self.get_devices(force_refresh=True)
        
        if not devices:
            raise MyQAPIError("No garage doors found")
        
        if device_id:
            for device in devices:
                if device.device_id == device_id:
                    return device
            raise MyQAPIError(f"Device {device_id} not found")
        
        return devices[0]
    
    def _get_covers(self) -> dict:
        """Get the covers from the selected account or all covers."""
        if self._selected_account_id and self._selected_account_id in self._api.accounts:
            return self._api.accounts[self._selected_account_id].covers
        return self._api.covers
    
    async def open_door(self, device_id: Optional[str] = None) -> bool:
        """Open a garage door."""
        await self.ensure_authenticated()
        
        covers = self._get_covers()
        
        if device_id is None:
            # Get first garage door
            if not covers:
                raise MyQAPIError("No garage doors found")
            device_id = list(covers.keys())[0]
        
        device = covers.get(device_id)
        if not device:
            raise MyQAPIError(f"Device {device_id} not found")
        
        try:
            await device.open()
            return True
        except MyQError as e:
            raise MyQAPIError(f"Failed to open door: {e}")
    
    async def close_door(self, device_id: Optional[str] = None) -> bool:
        """Close a garage door."""
        await self.ensure_authenticated()
        
        covers = self._get_covers()
        
        if device_id is None:
            if not covers:
                raise MyQAPIError("No garage doors found")
            device_id = list(covers.keys())[0]
        
        device = covers.get(device_id)
        if not device:
            raise MyQAPIError(f"Device {device_id} not found")
        
        try:
            await device.close()
            return True
        except MyQError as e:
            raise MyQAPIError(f"Failed to close door: {e}")


# Singleton client instance
_client: Optional[MyQClient] = None


def get_myq_client() -> MyQClient:
    """Get or create the MyQ client instance."""
    global _client
    if _client is None:
        settings = get_settings()
        _client = MyQClient(settings.myq_email, settings.myq_password)
    return _client


async def reset_client() -> None:
    """Reset the client (useful after auth failures)."""
    global _client
    if _client and _client._api:
        # Close the aiohttp session if it exists
        try:
            await _client._api._myqrequests._websession.close()
        except Exception:
            pass
    _client = None
