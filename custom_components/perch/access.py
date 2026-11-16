"""Who may reach the companion's views at all (local only by default).

Local only by default. A request through Home Assistant Cloud is allowed only
when the admin turned on "Allow Perch through Home Assistant Cloud", and never
for pairing. Anything else must come from a loopback, private or link-local
address. Behind a reverse proxy that hides the visitor's address, everyone
looks local; the README says so.
"""

from __future__ import annotations

from enum import StrEnum
from ipaddress import ip_address

from aiohttp import web

from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import is_cloud_connection
from homeassistant.util.network import is_local


class Origin(StrEnum):
    """Where a request came from, as far as the companion can tell."""

    LOCAL = "local"
    CLOUD = "cloud"
    REMOTE = "remote"


def request_origin(hass: HomeAssistant, request: web.Request) -> Origin:
    """Classify a request. Cloud is checked first: it arrives from loopback."""
    if is_cloud_connection(hass):
        return Origin.CLOUD
    remote = request.remote
    if not remote:
        return Origin.REMOTE
    try:
        address = ip_address(remote)
    except ValueError:
        return Origin.REMOTE
    return Origin.LOCAL if is_local(address) else Origin.REMOTE


def request_allowed(
    hass: HomeAssistant, request: web.Request, *, allow_cloud: bool
) -> bool:
    """Return True if this request may use the companion endpoint."""
    origin = request_origin(hass, request)
    return origin is Origin.LOCAL or (origin is Origin.CLOUD and allow_cloud)
