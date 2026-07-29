"""AWS Lambda entry point.

``lifespan="off"`` is deliberate: Mangum enters a lifespan context on every
invocation, so ASGI startup/shutdown events would run per request and discard
the HTTP session, token cache and device cache each time. ``myq.api`` builds
that state lazily at module scope instead, so it persists for the life of the
warm container.
"""

import logging
import os

from mangum import Mangum

from .api import app

logging.getLogger().setLevel(os.getenv("LOG_LEVEL", "INFO"))

handler = Mangum(app, lifespan="off")
