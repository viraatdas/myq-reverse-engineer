"""Typed errors that map cleanly onto HTTP status codes.

The old implementation turned every failure into a 500 with the raw exception
string in the body. Callers (and Shortcuts) could not tell "your tokens
expired, go re-auth" apart from "MyQ is down", and internal detail leaked to
anyone who could reach the endpoint.
"""


class MyQError(Exception):
    """Base class for all MyQ failures.

    ``status`` is the HTTP code to surface and ``message`` is safe to return
    to the caller — it never contains tokens or upstream response bodies.
    """

    status = 500
    message = "Unexpected MyQ error"

    def __init__(self, message: str | None = None, *, detail: str | None = None):
        self.message = message or self.message
        # Logged server-side only, never returned in a response body.
        self.detail = detail
        super().__init__(self.message)


class NotConfigured(MyQError):
    """The service is missing required configuration (e.g. no API key)."""

    status = 503
    message = "Service is not configured"


class ReauthRequired(MyQError):
    """Tokens are missing or unrecoverable; a human must log in again.

    Distinct from a transient upstream failure: retrying will not help.
    """

    status = 503
    message = (
        "MyQ authentication expired. Run 'python -m myq.cli login' locally, "
        "then 'python -m myq.cli push-tokens' to upload the new tokens."
    )


class InvalidApiKey(MyQError):
    """Caller presented a missing or wrong API key."""

    status = 401
    message = "Invalid or missing API key"


class RateLimited(MyQError):
    """Caller exceeded this service's own per-IP request budget."""

    status = 429
    message = "Too many requests"


class UpstreamError(MyQError):
    """MyQ's own API returned an error or was unreachable."""

    status = 502
    message = "MyQ service is unavailable"


class DoorOffline(MyQError):
    """The opener is not reporting to MyQ, so commands would silently vanish."""

    status = 409
    message = "The garage door opener is offline"


class DeviceNotFound(MyQError):
    """No matching garage door on the account."""

    status = 404
    message = "No garage door found on this MyQ account"


class CommandTimeout(MyQError):
    """The door did not reach the requested state within the wait window.

    504 rather than 500: the command was accepted, confirmation just timed out.
    """

    status = 504
    message = "Door did not reach the requested state in time"
