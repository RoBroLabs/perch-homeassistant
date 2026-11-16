"""The TV's WebSocket: `GET /api/perch/ws`. One call of `handle_ws` is one
connection. Nothing here logs a credential, proof, key or challenge, and a
failed auth never reaches Home Assistant's IP ban."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
from typing import Any

from aiohttp import WSCloseCode, WSMsgType, web
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from homeassistant.core import HomeAssistant

from .access import Origin, request_allowed, request_origin
from .commands import CommandError, Revoked, run_command
from .const import (
    AUTH_TIMEOUT,
    CLOSE_AUTH_INVALID,
    CLOSE_REVOKED,
    CONF_ALLOW_CLOUD,
    COMPANION_VERSION,
    CONF_CREDENTIAL_HASH,
    CONF_PUBLIC_KEY,
    CONF_TV_ID,
    HEARTBEAT,
    PROTOCOL_VERSION,
    SUBENTRY_TV,
)
from .crypto import open_auth_proof
from .runtime import PerchRuntime, get_runtime

_LOGGER = logging.getLogger(__name__)


def _b64(value: Any) -> bytes | None:
    if not isinstance(value, str):
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None


def _lookup(runtime: PerchRuntime, tv_id: Any) -> tuple[bytes | None, str | None]:
    """The stored public key and hash for a TV id, or (None, None)."""
    if isinstance(tv_id, str):
        for sub in runtime.entry.subentries.values():
            if sub.subentry_type == SUBENTRY_TV and sub.data.get(CONF_TV_ID) == tv_id:
                pub = _b64(sub.data.get(CONF_PUBLIC_KEY))
                if pub is not None and len(pub) == 32:
                    return pub, sub.data.get(CONF_CREDENTIAL_HASH)
    return None, None


def _auth_ok(runtime: PerchRuntime, server: X25519PrivateKey, challenge: bytes, frame: Any) -> str | None:
    """The bound tv_id if `frame` is a valid auth frame, else None. Unknown
    TVs and wrong proofs take the same path through `open_auth_proof`."""
    if not isinstance(frame, dict):
        return None
    proof = _b64(frame.get("proof")) or b""
    tv_id = frame.get("tv_id")
    pub, stored = _lookup(runtime, tv_id)
    good = open_auth_proof(server, pub, stored, challenge, proof)
    if (
        frame.get("type") != "auth"
        or frame.get("protocol") != PROTOCOL_VERSION
        or not good
    ):
        return None
    return tv_id


async def _reject(ws: web.WebSocketResponse) -> None:
    await ws.send_json({"type": "auth_invalid"})
    await ws.close(code=CLOSE_AUTH_INVALID)


async def handle_ws(
    hass: HomeAssistant, request: web.Request, runtime: PerchRuntime
) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=HEARTBEAT)
    origin = request_origin(hass, request)
    await ws.prepare(request)
    server = X25519PrivateKey.generate()
    challenge = os.urandom(32)
    await ws.send_json(
        {
            "type": "auth_required",
            "protocol": PROTOCOL_VERSION,
            "challenge": base64.b64encode(challenge).decode(),
            "server_key": base64.b64encode(
                server.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            ).decode(),
        }
    )
    try:
        msg = await ws.receive(timeout=AUTH_TIMEOUT)
    except TimeoutError:
        await _reject(ws)
        return ws
    frame = None
    if msg.type is WSMsgType.TEXT:
        try:
            frame = json.loads(msg.data)
        except ValueError:
            frame = None
    elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
        return ws
    # Always run the proof check, even for a non-auth frame, so every failure
    # costs the same.
    tv_id = _auth_ok(runtime, server, challenge, frame)
    # The integration may have been unloaded or reloaded while the TV was
    # authenticating: this connection must not be served by a dead runtime.
    if tv_id is None or get_runtime(hass) is not runtime:
        await _reject(ws)
        return ws
    if not _still_allowed(hass, request, runtime, origin):
        await ws.close(code=CLOSE_REVOKED)
        return ws

    runtime.sockets.setdefault(tv_id, set()).add(ws)
    if runtime.moods is not None:
        runtime.moods.tv_connected(tv_id)
    runtime.origins[ws] = origin
    try:
        await ws.send_json(
            {
                "type": "auth_ok",
                "protocol": PROTOCOL_VERSION,
                "companion_version": COMPANION_VERSION,
            }
        )
        async for msg in ws:
            if msg.type is not WSMsgType.TEXT:
                continue
            try:
                frame = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(frame, dict):
                continue
            if not await _dispatch(hass, request, runtime, ws, tv_id, frame):
                break
    except (ConnectionError, RuntimeError):
        pass  # the TV went away mid-send
    finally:
        runtime.origins.pop(ws, None)
        if runtime.hub is not None:
            runtime.hub.drop(ws)
        sockets = runtime.sockets.get(tv_id)
        if sockets is not None:
            sockets.discard(ws)
            if not sockets:
                runtime.sockets.pop(tv_id, None)
                if runtime.moods is not None:
                    runtime.moods.tv_disconnected(tv_id)
    return ws


def _still_allowed(
    hass: HomeAssistant, request: web.Request, runtime: PerchRuntime, origin: Origin
) -> bool:
    """Is this connection still allowed to reach the companion? The request
    context variable that says Cloud persists in the handler task."""
    allow_cloud = runtime.entry.options.get(CONF_ALLOW_CLOUD, False)
    return request_allowed(hass, request, allow_cloud=allow_cloud) and not (
        origin is Origin.CLOUD and not allow_cloud
    )


async def _dispatch(
    hass: HomeAssistant,
    request: web.Request,
    runtime: PerchRuntime,
    ws: web.WebSocketResponse,
    tv_id: str,
    frame: dict[str, Any],
) -> bool:
    """Answer one frame. False means the connection has been closed."""
    if get_runtime(hass) is not runtime:
        await ws.close(code=WSCloseCode.GOING_AWAY)
        return False
    if not _still_allowed(hass, request, runtime, runtime.origins.get(ws, Origin.LOCAL)):
        await ws.close(code=CLOSE_REVOKED)
        return False
    cid = frame.get("id")
    if not isinstance(cid, int) or isinstance(cid, bool) or cid < 1:
        return True  # nothing to answer to
    ftype = frame.get("type")
    if ftype == "ping":
        await ws.send_json({"id": cid, "type": "pong"})
        return True
    reply: dict[str, Any] = {"id": cid, "type": "result"}
    if not isinstance(ftype, str):
        reply.update(success=False, error={"code": "unknown_command", "message": "unknown command"})
        await ws.send_json(reply)
        return True
    try:
        result = await run_command(hass, runtime.entry, tv_id, frame, ws)
    except CommandError as err:
        reply.update(success=False, error={"code": err.code, "message": err.message})
    except Revoked:
        await revoke_sockets(runtime, tv_id)
        return False
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Perch command failed")
        reply.update(success=False, error={"code": "unavailable", "message": "command failed"})
    else:
        reply.update(success=True, result=result)
    await ws.send_json(reply)
    return True


async def close_cloud_sockets(runtime: PerchRuntime) -> None:
    """Close every open Cloud socket (Cloud access was turned off)."""
    for ws, origin in list(runtime.origins.items()):
        if origin is Origin.CLOUD and not ws.closed:
            try:
                await ws.close(code=CLOSE_REVOKED)
            except (ConnectionError, RuntimeError):
                pass


async def revoke_sockets(runtime: PerchRuntime, tv_id: str) -> None:
    """Tell every open socket of this TV it was removed, then close it."""
    for ws in list(runtime.sockets.pop(tv_id, ())):
        if ws.closed:
            continue
        try:
            await ws.send_json({"type": "revoked"})
            await ws.close(code=CLOSE_REVOKED)
        except (ConnectionError, RuntimeError, asyncio.CancelledError):
            pass


async def close_all_sockets(runtime: PerchRuntime) -> None:
    """Close every open socket (unload)."""
    sockets = [ws for group in runtime.sockets.values() for ws in group]
    runtime.sockets.clear()
    for ws in sockets:
        if not ws.closed:
            await ws.close(code=WSCloseCode.GOING_AWAY)
