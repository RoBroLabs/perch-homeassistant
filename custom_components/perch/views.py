"""HTTP views. Home Assistant authentication is off by design;
each view does its own access check and throttling, and never reports a
failure to Home Assistant's IP ban. Wire shapes follow `protocol/`."""

from __future__ import annotations

import base64
import binascii
from http import HTTPStatus
import logging
import math
from typing import Any

from aiohttp import web

from homeassistant.helpers.http import HomeAssistantView

from .access import Origin, request_allowed, request_origin
from .const import CONF_ALLOW_CLOUD, PAIRING_TTL
from .endpoint import handle_ws
from .pairing import TV_NAME_MAX, BusyError, PairingStatus, RateLimiter, _now
from .runtime import PerchRuntime, get_runtime

_LOGGER = logging.getLogger(__name__)

PAIR_URL = "/api/perch/pair"
PAIR_STATUS_URL = "/api/perch/pair/{pairing_id}"
WS_URL = "/api/perch/ws"
PROTOCOL_VERSION = 1


def _b64(value: Any, length: int) -> bytes:
    if not isinstance(value, str):
        raise ValueError
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as err:
        raise ValueError from err
    if len(raw) != length:
        raise ValueError
    return raw


def _source(request: web.Request) -> str | None:
    """The address a pairing request came from."""
    return request.remote


def _error(
    view: HomeAssistantView,
    code: str,
    status: HTTPStatus,
    retry_after: int | None = None,
) -> web.Response:
    body: dict[str, Any] = {"error": code}
    headers = None
    if retry_after is not None:
        body["retry_after"] = retry_after
        headers = {"Retry-After": str(retry_after)}
    return view.json(body, status_code=status, headers=headers)


def _gate(
    view: HomeAssistantView, request: web.Request, limiter_of
) -> tuple[PerchRuntime | None, web.Response | None]:
    """Shared checks for the pairing views: loaded, local, not throttled."""
    hass = request.app["hass"]
    runtime = get_runtime(hass)
    if runtime is None:
        return None, _error(view, "not_found", HTTPStatus.NOT_FOUND)
    origin = request_origin(hass, request)
    if origin is Origin.CLOUD:
        return None, _error(view, "pairing_local_only", HTTPStatus.FORBIDDEN)
    if origin is not Origin.LOCAL:
        return None, _error(view, "not_local", HTTPStatus.FORBIDDEN)
    limiter: RateLimiter = limiter_of(runtime)
    if wait := limiter.check(request.remote):
        return None, _error(
            view, "rate_limited", HTTPStatus.TOO_MANY_REQUESTS, retry_after=wait
        )
    return runtime, None


class PairView(HomeAssistantView):
    """A TV asks to pair: `POST /api/perch/pair`."""

    url = PAIR_URL
    name = "api:perch:pair"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        runtime, refusal = _gate(self, request, lambda r: r.pair_limiter)
        if refusal is not None:
            return refusal
        assert runtime is not None
        try:
            body = await request.json()
        except ValueError:
            return _error(self, "bad_request", HTTPStatus.BAD_REQUEST)
        if not isinstance(body, dict) or "protocol" not in body:
            return _error(self, "bad_request", HTTPStatus.BAD_REQUEST)
        protocol = body["protocol"]
        if not isinstance(protocol, int) or isinstance(protocol, bool):
            return _error(self, "bad_request", HTTPStatus.BAD_REQUEST)
        if protocol != PROTOCOL_VERSION:
            return _error(self, "unsupported_protocol", HTTPStatus.BAD_REQUEST)
        try:
            tv_name = body["tv_name"]
            if not isinstance(tv_name, str):
                raise ValueError
            tv_name = tv_name.strip()
            if not 0 < len(tv_name) <= TV_NAME_MAX or not tv_name.isprintable():
                raise ValueError
            public_key = _b64(body["public_key"], 32)
            nonce = _b64(body["nonce"], 16)
            mac = _b64(body["hmac"], 32)
        except (ValueError, KeyError, TypeError):
            return _error(self, "bad_request", HTTPStatus.BAD_REQUEST)
        try:
            contender = runtime.pairing.start(
                tv_name, public_key, nonce, mac, _source(request)
            )
        except BusyError:
            return _error(self, "busy", HTTPStatus.CONFLICT)
        # Seconds left in the window: less than 300 for a TV that joins late.
        window = runtime.pairing.window
        left = PAIRING_TTL if window is None else window.expires_at - _now()
        expires_in = max(1, min(PAIRING_TTL, math.ceil(left)))
        return self.json({"pairing_id": contender.pairing_id, "expires_in": expires_in})


class WsView(HomeAssistantView):
    """The TV's WebSocket: `GET /api/perch/ws`."""

    url = WS_URL
    name = "api:perch:ws"
    requires_auth = False

    async def get(self, request: web.Request) -> web.StreamResponse:
        hass = request.app["hass"]
        runtime = get_runtime(hass)
        if runtime is None:
            return _error(self, "not_found", HTTPStatus.NOT_FOUND)
        allow_cloud = runtime.entry.options.get(CONF_ALLOW_CLOUD, False)
        if not request_allowed(hass, request, allow_cloud=allow_cloud):
            return _error(self, "not_allowed", HTTPStatus.FORBIDDEN)
        if wait := runtime.auth_limiter.check(request.remote):
            return _error(
                self, "rate_limited", HTTPStatus.TOO_MANY_REQUESTS, retry_after=wait
            )
        return await handle_ws(hass, request, runtime)


class PairStatusView(HomeAssistantView):
    """A TV polls its pairing: `GET /api/perch/pair/{pairing_id}`."""

    url = PAIR_STATUS_URL
    name = "api:perch:pair_status"
    requires_auth = False

    async def get(self, request: web.Request, pairing_id: str) -> web.Response:
        runtime, refusal = _gate(self, request, lambda r: r.poll_limiter)
        if refusal is not None:
            return refusal
        assert runtime is not None
        contender = runtime.pairing.lookup(pairing_id)
        if contender is None:
            return _error(self, "not_found", HTTPStatus.NOT_FOUND)
        status = runtime.pairing.status(contender)
        if status is PairingStatus.APPROVED:
            runtime.pairing.delivered(contender)
            return self.json(
                {
                    "status": status.value,
                    "tv_id": contender.tv_id,
                    "envelope": base64.b64encode(contender.envelope or b"").decode(),
                }
            )
        return self.json({"status": status.value})
