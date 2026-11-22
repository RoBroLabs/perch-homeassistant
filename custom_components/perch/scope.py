"""One TV's scope and manifest. The only place that decides what is curated.

Everything here is computed from the entry and the TV id the caller already
authenticated. No other source of a TV id is accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .runtime import get_runtime
from .const import (
    CONF_CAMERAS,
    CONF_DOORBELL,
    CONF_DOORBELL_CAMERA,
    CONF_MOVIE_SCENE,
    CONF_PAUSED_SCENE,
    CONF_ROOM_ID,
    CONF_ROOMS,
    CONF_SCENES,
    CONF_TV_ID,
    SUBENTRY_TV,
)

_NOT_DIMMABLE = {"onoff", "unknown"}


@dataclass(frozen=True)
class TvScope:
    tv_id: str
    tv_name: str
    room_id: str | None
    rooms: tuple[str, ...]
    lights: frozenset[str]
    cameras: frozenset[str]
    doorbell: str | None
    scenes: frozenset[str]
    doorbell_camera: str | None = None
    movie_scene: str | None = None
    paused_scene: str | None = None


def _area_of(entity_id: str, ent_reg: er.EntityRegistry, dev_reg: dr.DeviceRegistry):
    """Entity area, else its device's area."""
    entry = ent_reg.async_get(entity_id)
    if entry is None:
        return None
    if entry.area_id:
        return entry.area_id
    if entry.device_id and (device := dev_reg.async_get(entry.device_id)):
        return device.area_id
    return None


def room_lights(hass: HomeAssistant, area_id: str) -> frozenset[str]:
    """Enabled lights whose own or device area is `area_id`."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    return frozenset(
        e.entity_id
        for e in ent_reg.entities.values()
        if e.domain == "light"
        and e.disabled_by is None
        and _area_of(e.entity_id, ent_reg, dev_reg) == area_id
    )


def build_scope(hass: HomeAssistant, entry: ConfigEntry, tv_id: str) -> TvScope | None:
    """The scope of the TV paired as `tv_id`, or None if there is no such TV."""
    subentry = next(
        (
            s
            for s in entry.subentries.values()
            if s.subentry_type == SUBENTRY_TV and s.data.get(CONF_TV_ID) == tv_id
        ),
        None,
    )
    if subentry is None:
        return None
    room_id = subentry.data.get(CONF_ROOM_ID) or None
    rooms: list[str] = [room_id] if room_id else []
    for area in subentry.data.get(CONF_ROOMS, []):
        if area not in rooms:
            rooms.append(area)

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    lights = frozenset(
        e.entity_id
        for e in ent_reg.entities.values()
        if e.domain == "light"
        and e.disabled_by is None
        and _area_of(e.entity_id, ent_reg, dev_reg) in rooms
    )
    options = entry.options
    cameras = frozenset(options.get(CONF_CAMERAS, []))
    scenes = frozenset(options.get(CONF_SCENES, []))
    doorbell = options.get(CONF_DOORBELL) or None
    doorbell_camera = options.get(CONF_DOORBELL_CAMERA)
    movie = options.get(CONF_MOVIE_SCENE)
    paused = options.get(CONF_PAUSED_SCENE)
    return TvScope(
        tv_id=tv_id,
        tv_name=subentry.title,
        room_id=room_id,
        rooms=tuple(rooms),
        lights=lights,
        cameras=cameras,
        doorbell=doorbell,
        scenes=scenes,
        doorbell_camera=doorbell_camera
        if doorbell and doorbell_camera in cameras
        else None,
        movie_scene=movie if movie in scenes else None,
        paused_scene=paused if paused in scenes and paused != movie else None,
    )


def is_allowed(scope: TvScope, entity_id: str) -> bool:
    """True only for an entity this TV may see or use."""
    return (
        entity_id in scope.lights
        or entity_id in scope.cameras
        or entity_id in scope.scenes
        or entity_id == scope.doorbell
    )


def _name(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    if state and state.name:
        return state.name
    entry = er.async_get(hass).async_get(entity_id)
    return (entry and (entry.name or entry.original_name)) or entity_id


def build_manifest(
    hass: HomeAssistant, entry: ConfigEntry, scope: TvScope
) -> dict[str, Any]:
    """The `perch/manifest` result for this scope."""
    area_reg = ar.async_get(hass)
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    def room(area_id: str) -> dict[str, str]:
        area = area_reg.async_get_area(area_id)
        return {"id": area_id, "name": area.name if area else area_id}

    lights = []
    for entity_id in sorted(scope.lights):
        state = hass.states.get(entity_id)
        modes = set((state.attributes.get("supported_color_modes") or []) if state else [])
        lights.append(
            {
                "id": entity_id,
                "name": _name(hass, entity_id),
                "room_id": _area_of(entity_id, ent_reg, dev_reg),
                "dimmable": bool(modes - _NOT_DIMMABLE),
            }
        )
    cameras = [
        {
            "id": entity_id,
            "name": _name(hass, entity_id),
            "room_id": _area_of(entity_id, ent_reg, dev_reg),
        }
        for entity_id in sorted(scope.cameras)
    ]
    scenes = [
        {
            "id": entity_id,
            "name": _name(hass, entity_id),
            "purpose": "movie_night"
            if entity_id == scope.movie_scene
            else "paused"
            if entity_id == scope.paused_scene
            else None,
        }
        for entity_id in sorted(scope.scenes)
    ]
    return {
        "tv_id": scope.tv_id,
        "tv_name": scope.tv_name,
        "room": room(scope.room_id) if scope.room_id else None,
        "rooms": [room(a) for a in scope.rooms],
        "lights": lights,
        "cameras": cameras,
        "doorbell": {"camera_id": scope.doorbell_camera} if scope.doorbell else None,
        "scenes": scenes,
        "let_in_available": False,
        "notify_available": _notify_available(hass),
    }


def _notify_available(hass: HomeAssistant) -> bool:
    runtime = get_runtime(hass)
    return bool(runtime and runtime.notifier and runtime.notifier.request_ready_available())


def role_may_control_lights(role: str) -> bool:
    """The single place for the companion's kid rule for lights: Kids profiles
    get no house controls, lights included. The protocol
    carries no role on `perch/light`, `perch/scene` or `perch/playback`, so no
    command calls this today; the Perch app enforces the rule. It is kept
    here so the rule changes in one place."""
    return role in ("adult", "teen")


def role_may_view_events(role: str) -> bool:
    """Kids never see camera events (used by `perch/events`)."""
    return role != "kid"
