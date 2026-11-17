"""Per-entry runtime state, reachable from the always-registered views."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .access import Origin
from .const import DOMAIN
from .pairing import PairingManager, RateLimiter

if TYPE_CHECKING:
    from .hub import PushHub
    from .moods import MoodEngine
    from aiohttp import web

# Throttle numbers. The README's rate-limit table lists them; keep the two in step.
PAIR_POST_PER_SOURCE = 10  # pairing starts a minute per address
PAIR_POST_OVERALL = 30  # pairing starts a minute, all addresses
PAIR_POLL_PER_SOURCE = 120  # status polls a minute per address
PAIR_POLL_OVERALL = 360  # status polls a minute, all addresses
AUTH_PER_SOURCE = 10  # endpoint connections a minute per address
AUTH_OVERALL = 60  # endpoint connections a minute, all addresses
BACKOFF_START = 60.0  # first block, seconds; doubles per breach
BACKOFF_MAX = 900.0  # longest block, seconds


@dataclass
class PerchRuntime:
    """What the views need while the entry is loaded."""

    entry: ConfigEntry
    pairing: PairingManager = field(default_factory=PairingManager)
    pair_limiter: RateLimiter = field(
        default_factory=lambda: RateLimiter(
            PAIR_POST_PER_SOURCE,
            PAIR_POST_OVERALL,
            backoff_start=BACKOFF_START,
            backoff_max=BACKOFF_MAX,
        )
    )
    poll_limiter: RateLimiter = field(
        default_factory=lambda: RateLimiter(
            PAIR_POLL_PER_SOURCE,
            PAIR_POLL_OVERALL,
            backoff_start=BACKOFF_START,
            backoff_max=BACKOFF_MAX,
        )
    )
    auth_limiter: RateLimiter = field(
        default_factory=lambda: RateLimiter(
            AUTH_PER_SOURCE,
            AUTH_OVERALL,
            backoff_start=BACKOFF_START,
            backoff_max=BACKOFF_MAX,
        )
    )
    sockets: dict[str, set[web.WebSocketResponse]] = field(default_factory=dict)
    # Where each open socket came from, so Cloud ones can be closed when the
    # admin turns Cloud access off.
    origins: dict[web.WebSocketResponse, Origin] = field(default_factory=dict)
    moods: MoodEngine | None = None
    hub: PushHub | None = None


@callback
def get_runtime(hass: HomeAssistant) -> PerchRuntime | None:
    """The loaded entry's runtime, or None (views stay registered after unload)."""
    return hass.data.get(DOMAIN)
