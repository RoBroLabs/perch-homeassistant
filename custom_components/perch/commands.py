"""The `perch/*` commands: manifest, states and camera.

Every command takes the TV id the connection was bound to by the auth handshake
and builds that TV's scope itself. Nothing in a frame is ever used for scope.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant
from homeassistant.util import dt as dt_util

from .const import SKIP_MAX_HOURS
from .runtime import get_runtime
from .scope import (
    TvScope,
    build_manifest,
    build_scope,
    is_allowed,
    role_may_view_events,
)

_LOGGER = logging.getLogger(__name__)

# The topics `perch/subscribe` accepts (schema `Topic`).
TOPICS = ("doorbell", "events", "states", "manifest")
URL_LIFETIME = timedelta(minutes=5)
# Valid in the schema, not supported yet.
RESERVED = {
    "perch/unpair",
    "perch/let_in",
}


class CommandError(Exception):
    """A refusal that becomes an error result frame."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Revoked(Exception):
    """The bound TV no longer exists."""


def _cmd_manifest(
    hass: HomeAssistant, entry: ConfigEntry, scope: TvScope, frame: dict[str, Any]
) -> dict[str, Any]:
    return build_manifest(hass, entry, scope)


def light_state(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """One `LightState`, shared by `perch/states` and the states push."""
    state = hass.states.get(entity_id)
    available = state is not None and state.state not in ("unavailable", "unknown")
    raw = state.attributes.get("brightness") if state else None
    brightness = None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        brightness = max(0, min(100, round(raw * 100 / 255)))
    return {
        "id": entity_id,
        "on": bool(state and state.state == "on"),
        "brightness": brightness,
        "available": available,
    }


def _cmd_states(
    hass: HomeAssistant, entry: ConfigEntry, scope: TvScope, frame: dict[str, Any]
) -> dict[str, Any]:
    # Extra fields are ignored (protocol README): only the TV's curated lights.
    return {"lights": [light_state(hass, e) for e in sorted(scope.lights)]}


def _cmd_subscribe(
    hass: HomeAssistant, ws: Any, scope: TvScope, frame: dict[str, Any]
) -> dict[str, Any]:
    topics = frame.get("topics")
    if (
        not isinstance(topics, list)
        or not 0 < len(topics) <= len(TOPICS)
        or any(t not in TOPICS for t in topics)
        or len(set(topics)) != len(topics)
    ):
        raise CommandError("invalid_format", "topics must be known and unique")
    runtime = get_runtime(hass)
    if runtime is None or runtime.hub is None or ws is None:
        raise CommandError("unavailable", "subscriptions are not available")
    # tv_id comes from the authenticated socket only.
    return {"topics": runtime.hub.subscribe(ws, scope.tv_id, topics, frame["id"])}


def _user_context(hass: HomeAssistant) -> Context:
    """A fresh Context, registered with the mood engine as a TV's own change."""
    context = Context()
    runtime = get_runtime(hass)
    if runtime is not None and runtime.moods is not None:
        runtime.moods.note_user_context(context.id)
    return context


async def _cmd_light(hass: HomeAssistant, scope: TvScope, frame: dict[str, Any]) -> dict[str, Any]:
    light_id, on, brightness = frame.get("light_id"), frame.get("on"), frame.get("brightness")
    if (
        not isinstance(light_id, str)
        or not 0 < len(light_id) <= 128
        or (on is not None and not isinstance(on, bool))
        or (
            brightness is not None
            and (
                not isinstance(brightness, int)
                or isinstance(brightness, bool)
                or not 0 <= brightness <= 100
            )
        )
        or (on is None and brightness is None)
    ):
        raise CommandError("invalid_format", "light_id and on or brightness are required")
    if light_id not in scope.lights:
        raise CommandError("not_allowed", "light not allowed for this TV")
    # A fresh Context that the mood engine does not know: a TV's own change of
    # a light counts as a hand change for moods.
    context = _user_context(hass)
    if on is False or (on is None and brightness == 0):
        await hass.services.async_call(
            "light", "turn_off", {"entity_id": light_id}, blocking=True, context=context
        )
    else:
        data: dict[str, Any] = {"entity_id": light_id}
        if brightness is not None:
            data["brightness_pct"] = brightness
        await hass.services.async_call(
            "light", "turn_on", data, blocking=True, context=context
        )
    return {}


async def _cmd_scene(hass: HomeAssistant, scope: TvScope, frame: dict[str, Any]) -> dict[str, Any]:
    scene_id = frame.get("scene_id")
    if not isinstance(scene_id, str) or not 0 < len(scene_id) <= 128:
        raise CommandError("invalid_format", "scene_id is required")
    if scene_id not in scope.scenes:
        raise CommandError("not_allowed", "scene not allowed for this TV")
    await hass.services.async_call(
        "scene", "turn_on", {"entity_id": scene_id}, blocking=True,
        context=_user_context(hass),
    )
    return {}


async def _cmd_camera(
    hass: HomeAssistant, entry: ConfigEntry, scope: TvScope, frame: dict[str, Any]
) -> dict[str, Any]:
    camera_id, kind = frame.get("camera_id"), frame.get("kind")
    if (
        not isinstance(camera_id, str)
        or not 0 < len(camera_id) <= 128
        or kind not in ("snapshot", "stream")
    ):
        raise CommandError("invalid_format", "camera_id and kind are required")
    if camera_id not in scope.cameras or not is_allowed(scope, camera_id):
        raise CommandError("not_allowed", "camera not allowed for this TV")
    state = hass.states.get(camera_id)
    if state is None or state.state == "unavailable":
        raise CommandError("unavailable", "camera unavailable")
    expires = dt_util.utcnow() + URL_LIFETIME
    if kind == "snapshot":
        token = state.attributes.get("access_token")
        if not token:
            raise CommandError("unavailable", "camera has no snapshot")
        url = f"/api/camera_proxy/{camera_id}?token={token}"
    else:
        try:
            from homeassistant.components.camera import async_request_stream

            url = await async_request_stream(hass, camera_id, "hls")
        except Exception:  # noqa: BLE001 - any failure is "no stream right now"
            _LOGGER.debug("No stream for %s", camera_id)
            raise CommandError("unavailable", "camera has no stream") from None
        if not url.startswith("/"):
            raise CommandError("unavailable", "camera has no stream")
    return {"url": url, "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ")}


_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


async def _cmd_events(
    hass: HomeAssistant, scope: TvScope, frame: dict[str, Any]
) -> dict[str, Any]:
    day, role, camera_id = frame.get("day"), frame.get("role"), frame.get("camera_id")
    if (
        not isinstance(day, str)
        or not _DAY.match(day)
        or role not in ("adult", "teen", "kid")
        or (camera_id is not None and (not isinstance(camera_id, str) or not camera_id))
    ):
        raise CommandError("invalid_format", "day and role are required")
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise CommandError("invalid_format", "day is not a date") from None
    # The kid rule comes first, so a Kids profile learns nothing else.
    if not role_may_view_events(role):
        raise CommandError("refused_for_role", "not available for this profile")
    if camera_id is not None and camera_id not in scope.cameras:
        raise CommandError("not_allowed", "camera not allowed for this TV")
    # Camera events are not in this release: there is no source for them yet
    # that Home Assistant or a camera integration exposes publicly.
    raise CommandError("unavailable", "events are not available")


_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


async def _cmd_playback(hass: HomeAssistant, scope: TvScope, frame: dict[str, Any]) -> dict[str, Any]:
    if (
        frame.get("state") not in ("playing", "paused", "stopped")
        or frame.get("media") not in ("film", "episode", "music", "other")
        or not isinstance(frame.get("lighting"), bool)
    ):
        raise CommandError("invalid_format", "state, media and lighting are required")
    moods = _moods(hass)
    return await moods.async_playback(
        scope, frame["state"], frame["media"], frame["lighting"]
    )


async def _cmd_skip_tonight(
    hass: HomeAssistant, scope: TvScope, frame: dict[str, Any]
) -> dict[str, Any]:
    raw = frame.get("until")
    until: datetime | None = None
    if isinstance(raw, str) and _TIMESTAMP.match(raw):
        try:
            until = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=dt_util.UTC
            )
        except ValueError:
            until = None
    now = dt_util.utcnow()
    if until is None or until <= now or until > now + timedelta(hours=SKIP_MAX_HOURS):
        raise CommandError(
            "invalid_format", "until must be within the next 24 hours, as UTC"
        )
    await _moods(hass).async_skip_tonight(scope, until)
    return {}


def _moods(hass: HomeAssistant):
    runtime = get_runtime(hass)
    if runtime is None or runtime.moods is None:
        raise CommandError("unavailable", "lighting is not available")
    return runtime.moods


async def run_command(
    hass: HomeAssistant,
    entry: ConfigEntry,
    tv_id: str,
    frame: dict[str, Any],
    ws: Any = None,
) -> dict[str, Any]:
    """Run one command for the bound TV. Returns the result, or raises
    CommandError (an error frame) or Revoked (the TV is gone)."""
    ctype = frame.get("type")
    if ctype in RESERVED:
        raise CommandError("not_supported_yet", "not built yet")
    if ctype not in (
        "perch/manifest",
        "perch/states",
        "perch/camera",
        "perch/playback",
        "perch/skip_tonight",
        "perch/subscribe",
        "perch/light",
        "perch/scene",
        "perch/events",
    ):
        raise CommandError("unknown_command", "unknown command")
    scope = build_scope(hass, entry, tv_id)
    if scope is None:
        raise Revoked
    if ctype == "perch/manifest":
        return _cmd_manifest(hass, entry, scope, frame)
    if ctype == "perch/playback":
        return await _cmd_playback(hass, scope, frame)
    if ctype == "perch/skip_tonight":
        return await _cmd_skip_tonight(hass, scope, frame)
    if ctype == "perch/subscribe":
        return _cmd_subscribe(hass, ws, scope, frame)
    if ctype == "perch/light":
        return await _cmd_light(hass, scope, frame)
    if ctype == "perch/scene":
        return await _cmd_scene(hass, scope, frame)
    if ctype == "perch/events":
        return await _cmd_events(hass, scope, frame)
    if ctype == "perch/states":
        return _cmd_states(hass, entry, scope, frame)
    return await _cmd_camera(hass, entry, scope, frame)
