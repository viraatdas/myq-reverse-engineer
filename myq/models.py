"""Response models.

Every model carries an ``as_text()`` rendering. iOS Shortcuts has no ergonomic
JSON parsing — being able to ask for ``?format=text`` and pipe the result
straight into a notification or "Speak Text" action is the difference between
a one-action shortcut and a six-action one.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from .client import DoorState


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class DoorStatus(BaseModel):
    name: str = Field(examples=["Garage Door"])
    serial_number: str
    state: str = Field(description="open, closed, opening, closing, or unknown")
    is_open: bool
    is_closed: bool
    is_moving: bool
    online: bool
    last_update: str = ""
    last_status: str = ""
    timestamp: str = Field(default_factory=utcnow)

    @classmethod
    def from_state(cls, state: DoorState) -> "DoorStatus":
        return cls(
            name=state.name,
            serial_number=state.serial_number,
            state=state.state,
            is_open=state.is_open,
            is_closed=state.is_closed,
            is_moving=state.is_moving,
            online=state.online,
            last_update=state.last_update,
            last_status=state.last_status,
        )

    def as_text(self) -> str:
        offline = "" if self.online else " (offline)"
        return f"{self.name} is {self.state}{offline}"


class ActionResult(BaseModel):
    success: bool
    action: str = Field(description="open or close")
    name: str
    serial_number: str
    previous_state: str
    state: str = Field(description="State after the command; final state when confirmed")
    confirmed: bool = Field(
        description="True when the door was polled until it reached the target state"
    )
    message: str
    timestamp: str = Field(default_factory=utcnow)

    def as_text(self) -> str:
        return self.message


class DoorList(BaseModel):
    count: int
    doors: list[DoorStatus]
    timestamp: str = Field(default_factory=utcnow)

    def as_text(self) -> str:
        if not self.doors:
            return "No garage doors found"
        return "\n".join(f"{d.name}: {d.state}" for d in self.doors)


class DeviceList(BaseModel):
    count: int
    devices: list[dict]
    timestamp: str = Field(default_factory=utcnow)

    def as_text(self) -> str:
        return "\n".join(
            f"{d.get('name', '?')} [{d.get('device_family', '?')}] {d.get('serial_number', '')}"
            for d in self.devices
        ) or "No devices found"


class Health(BaseModel):
    status: str = Field(description="ok, degraded, or unconfigured")
    authenticated: bool
    api_key_configured: bool
    token_store: str
    token_expires_in: int | None = Field(
        default=None, description="Seconds until the access token expires"
    )
    version: str
    timestamp: str = Field(default_factory=utcnow)

    def as_text(self) -> str:
        return self.status


class ErrorBody(BaseModel):
    error: str
    detail: str = ""
    timestamp: str = Field(default_factory=utcnow)
