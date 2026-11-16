"""The mood engine. Dims a room for a film, lifts it on pause and puts
the lights back when the last TV in the room stops.

Everything is per room. A room holds a lease for each TV that is playing or
paused, a snapshot of the lights taken before Perch first changed them, the
values Perch left, the lights somebody changed by hand, and the mood applied.
All of it is persisted, so a restart in the middle of a film still restores.

Hand changes are told apart from Perch's own by context, with two allowances:
brightness within HAND_CHANGE_TOLERANCE of what Perch left is not a change, and
for HAND_CHANGE_GRACE seconds after a Perch service call on a room, changes from
other contexts (transitions, rounding, lights an integration scene moves outside
Perch's context) update the recorded Perch value instead.

Limit: a Movie night or Paused scene from another integration that has no
`entity_id` attribute cannot be inspected, so only the room's own lights are
snapshotted and restored for it.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, Event, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_RESTORE_TIMEOUT,
    DEFAULT_RESTORE_TIMEOUT,
    HAND_CHANGE_GRACE,
    HAND_CHANGE_TOLERANCE,
    MOOD_EPISODE_PCT,
    MOOD_FILM_PCT,
    MOOD_PAUSED_PCT,
    STORE_KEY,
    STORE_VERSION,
)
from .scope import TvScope, room_lights

_LOGGER = logging.getLogger(__name__)

_GONE = ("unavailable", "unknown")
_MAX_CONTEXTS = 500
# The colour attribute that goes with each colour mode.
_COLOUR_ATTR = {
    "color_temp": "color_temp_kelvin",
    "hs": "hs_color",
    "rgb": "rgb_color",
    "rgbw": "rgbw_color",
    "rgbww": "rgbww_color",
    "xy": "xy_color",
}


def _value(state: State) -> dict[str, Any]:
    """What matters for hand-change detection: on/off and brightness."""
    on = state.state == "on"
    brightness = state.attributes.get("brightness") if on else None
    return {"on": on, "brightness": brightness if isinstance(brightness, int) else None}


def _same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Equal on/off, and brightness within the tolerance."""
    if a["on"] != b["on"]:
        return False
    x, y = a["brightness"], b["brightness"]
    if isinstance(x, int) and isinstance(y, int):
        return abs(x - y) <= HAND_CHANGE_TOLERANCE
    return x == y


def _pct(brightness: int) -> int:
    return round(brightness * 100 / 255)


def _entity_snapshot(state: State) -> dict[str, Any]:
    """The state and restorable attributes of one light."""
    if state.state != "on":
        return {"state": "off"}
    out: dict[str, Any] = {"state": "on"}
    if (brightness := state.attributes.get("brightness")) is not None:
        out["brightness"] = brightness
    mode = state.attributes.get("color_mode")
    if mode:
        out["color_mode"] = mode
        attr = _COLOUR_ATTR.get(mode)
        if attr and state.attributes.get(attr) is not None:
            out[attr] = list(v) if isinstance(v := state.attributes[attr], tuple) else v
    return out


def _new_room() -> dict[str, Any]:
    return {
        "tvs": {},
        "snapshot": {},
        "perch_values": {},
        "hand_changed": [],
        "mood": None,
    }


class MoodEngine:
    """Room leases, snapshot, apply, restore and hand-change tracking."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, STORE_KEY)
        self._lock = asyncio.Lock()
        self._rooms: dict[str, dict[str, Any]] = {}
        self._skip: dict[str, str] = {}
        self._timers: dict[str, Callable[[], None]] = {}
        self._contexts: OrderedDict[str, None] = OrderedDict()
        self._unsub: Callable[[], None] | None = None
        self._watched: frozenset[str] = frozenset()
        self._unloaded = False
        self._user_contexts: OrderedDict[str, None] = OrderedDict()
        self._last_call: dict[str, float] = {}

    # --- lifecycle ---------------------------------------------------------

    async def async_load(self) -> None:
        """Read the store; every lease held across a restart gets a fresh timer."""
        data = await self._store.async_load() or {}
        rooms = data.get("rooms")
        if isinstance(rooms, dict):
            for room_id, raw in rooms.items():
                if not isinstance(raw, dict):
                    continue
                room = _new_room()
                for key in room:
                    if key in raw:
                        room[key] = raw[key]
                self._rooms[room_id] = room
        skip = data.get("skip")
        if isinstance(skip, dict):
            self._skip = {k: v for k, v in skip.items() if isinstance(v, str)}
        for tv_id in self._lease_tvs():
            self._start_timer(tv_id)
        self._resubscribe()

    async def async_unload(self) -> None:
        """Cancel listeners and timers, then write the store."""
        async with self._lock:  # an in-flight playback finishes first
            self._unloaded = True
            for cancel in self._timers.values():
                cancel()
            self._timers.clear()
            if self._unsub:
                self._unsub()
                self._unsub = None
            await self._store.async_save(self._data())

    def _data(self) -> dict[str, Any]:
        return {"rooms": self._rooms, "skip": self._skip}

    async def _save(self) -> None:
        if self._unloaded:
            return
        await self._store.async_save(self._data())

    # --- queries -----------------------------------------------------------

    def _lease_tvs(self) -> set[str]:
        return {tv for room in self._rooms.values() for tv in room["tvs"]}

    def known_tvs(self) -> set[str]:
        """Every TV the engine holds a lease or a skip-tonight for."""
        return self._lease_tvs() | set(self._skip)

    def room_of(self, tv_id: str) -> str | None:
        for room_id, room in self._rooms.items():
            if tv_id in room["tvs"]:
                return room_id
        return None

    def _skipped(self, tv_id: str) -> bool:
        raw = self._skip.get(tv_id)
        if raw is None:
            return False
        until = dt_util.parse_datetime(raw)
        if until is not None and until > dt_util.utcnow():
            return True
        del self._skip[tv_id]  # expired naturally
        return False

    def _after_sunset(self) -> bool:
        sun = self.hass.states.get("sun.sun")
        return sun is None or sun.state == "below_horizon"

    # --- timers and connection --------------------------------------------

    def tv_connected(self, tv_id: str) -> None:
        """A reconnect alone proves nothing: only a playback command cancels
        the restore timer, so a running timer is left to run."""

    def tv_disconnected(self, tv_id: str) -> None:
        """The TV's last socket closed: a lease it holds expires in the timeout."""
        if tv_id in self._lease_tvs():
            self._start_timer(tv_id)

    def _cancel_timer(self, tv_id: str) -> None:
        if cancel := self._timers.pop(tv_id, None):
            cancel()

    def _start_timer(self, tv_id: str) -> None:
        if self._unloaded:
            return
        self._cancel_timer(tv_id)
        minutes = self.entry.options.get(CONF_RESTORE_TIMEOUT, DEFAULT_RESTORE_TIMEOUT)

        async def expired(_now: datetime) -> None:
            self._timers.pop(tv_id, None)
            async with self._lock:
                if self._unloaded:
                    return
                await self._release(tv_id)

        self._timers[tv_id] = async_call_later(self.hass, minutes * 60, expired)

    # --- commands ----------------------------------------------------------

    async def async_playback(
        self, scope: TvScope, state: str, media: str, lighting: bool
    ) -> dict[str, Any]:
        tv_id = scope.tv_id
        async with self._lock:
            if self._unloaded:
                return {"applied": False, "reason": "unloaded"}
            self._cancel_timer(tv_id)
            reason: str | None = None
            if scope.room_id is None:
                reason = "no_room"
            elif not lighting:
                reason = "lighting_off"
            elif self._skipped(tv_id):
                reason = "skipped_tonight"
            elif not self._after_sunset():
                reason = "not_after_sunset"
            if reason or state == "stopped":
                await self._release(tv_id)
                return {"applied": False, "reason": reason} if reason else {
                    "applied": True,
                    "reason": None,
                }
            assert scope.room_id is not None
            await self._active(scope, scope.room_id, state, media)
            await self._save()
            return {"applied": True, "reason": None}

    async def async_skip_tonight(self, scope: TvScope, until: datetime) -> None:
        async with self._lock:
            self._skip[scope.tv_id] = until.strftime("%Y-%m-%dT%H:%M:%SZ")
            await self._release(scope.tv_id)

    async def async_release_tv(
        self, tv_id: str, keep_room: str | None = None, removed: bool = True
    ) -> None:
        """Release a TV's leases. `removed` (the TV is unpaired) also clears its
        skip-tonight; a TV that only left its room keeps it."""
        async with self._lock:
            await self._release(tv_id, keep_room=keep_room)
            if removed:
                self._skip.pop(tv_id, None)
                self._cancel_timer(tv_id)
                await self._save()

    # --- engine ------------------------------------------------------------

    async def _release(self, tv_id: str, keep_room: str | None = None) -> None:
        """Drop the TV's leases; restore every room that is left empty."""
        self._cancel_timer(tv_id)
        for room_id in list(self._rooms):
            room = self._rooms[room_id]
            if room_id == keep_room or tv_id not in room["tvs"]:
                continue
            del room["tvs"][tv_id]
            if not room["tvs"]:
                await self._restore(room_id)
        # The TV may still hold a lease in `keep_room` only.
        await self._save()

    async def _active(
        self, scope: TvScope, room_id: str, state: str, media: str
    ) -> None:
        room = self._rooms.setdefault(room_id, _new_room())
        room["tvs"][scope.tv_id] = state
        if state == "playing":
            if media == "film":
                await self._dim(scope, room_id, room, "film", MOOD_FILM_PCT, scope.movie_scene)
            elif media == "episode":
                await self._dim(scope, room_id, room, "episode", MOOD_EPISODE_PCT, None)
            return
        # Paused: another TV playing in the room keeps the room dark.
        if any(s == "playing" for tv, s in room["tvs"].items() if tv != scope.tv_id):
            return
        if scope.paused_scene:
            await self._apply_scene(scope, room_id, room, "paused", scope.paused_scene)
        elif room["mood"] in ("film", "episode"):
            await self._raise(room_id, room)

    def _managed(self, scope: TvScope, room_id: str) -> set[str]:
        """The room's lights plus the lights the two mood scenes touch."""
        ids = set(room_lights(self.hass, room_id))
        for scene_id in (scope.movie_scene, scope.paused_scene):
            if scene_id and (state := self.hass.states.get(scene_id)):
                ids.update(
                    e
                    for e in state.attributes.get("entity_id") or []
                    if isinstance(e, str) and e.startswith("light.")
                )
        return ids

    def _take_snapshot(self, scope: TvScope, room_id: str, room: dict[str, Any]) -> None:
        """Only when the room has no applied mood: never over a dimmed room."""
        if room["mood"] is not None or room["snapshot"]:
            return
        for entity_id in sorted(self._managed(scope, room_id)):
            state = self.hass.states.get(entity_id)
            if state is None or state.state in _GONE:
                continue
            room["snapshot"][entity_id] = _entity_snapshot(state)
        self._resubscribe()

    def _usable(self, room: dict[str, Any], entity_ids: set[str] | list[str]) -> list[str]:
        return [e for e in sorted(entity_ids) if e not in room["hand_changed"]]

    async def _dim(
        self,
        scope: TvScope,
        room_id: str,
        room: dict[str, Any],
        mood: str,
        pct: int,
        scene_id: str | None,
    ) -> None:
        if scene_id:
            await self._apply_scene(scope, room_id, room, mood, scene_id)
            return
        self._take_snapshot(scope, room_id, room)
        room["mood"] = mood
        targets = []
        for entity_id in self._usable(room, room_lights(self.hass, room_id)):
            state = self.hass.states.get(entity_id)
            if state is None or state.state != "on":
                continue
            brightness = state.attributes.get("brightness")
            if isinstance(brightness, int) and _pct(brightness) > pct:
                targets.append(entity_id)
        if targets:
            await self._call(
                room_id, "light", "turn_on", {"entity_id": targets, "brightness_pct": pct}
            )

    async def _apply_scene(
        self, scope: TvScope, room_id: str, room: dict[str, Any], mood: str, scene_id: str
    ) -> None:
        if room["mood"] == mood:
            return
        state = self.hass.states.get(scene_id)
        lights = {
            e
            for e in (state.attributes.get("entity_id") or [] if state else [])
            if isinstance(e, str) and e.startswith("light.")
        }
        if lights & set(room["hand_changed"]):
            # A scene cannot skip a light, so a hand-changed one holds it back.
            return
        self._take_snapshot(scope, room_id, room)
        room["mood"] = mood
        await self._call(room_id, "scene", "turn_on", {"entity_id": scene_id})

    async def _raise(self, room_id: str, room: dict[str, Any]) -> None:
        """Bring up only the lights Perch dimmed, to the paused level."""
        room["mood"] = "paused"
        targets = []
        for entity_id in self._usable(room, room["snapshot"]):
            snap = room["snapshot"][entity_id]
            state = self.hass.states.get(entity_id)
            if (
                state is None
                or state.state != "on"
                or entity_id not in room["perch_values"]
                or not isinstance(snap.get("brightness"), int)
            ):
                continue
            brightness = state.attributes.get("brightness")
            if (
                isinstance(brightness, int)
                and _pct(brightness) < MOOD_PAUSED_PCT < _pct(snap["brightness"])
            ):
                targets.append(entity_id)
        if targets:
            await self._call(
                room_id,
                "light",
                "turn_on",
                {"entity_id": targets, "brightness_pct": MOOD_PAUSED_PCT},
            )

    async def _restore(self, room_id: str) -> None:
        """Put the snapshot back, skipping hand-changed lights, then forget the room."""
        room = self._rooms.pop(room_id, None)
        if room is None:
            return
        entities = {
            entity_id: snap
            for entity_id, snap in room["snapshot"].items()
            if entity_id not in room["hand_changed"]
        }
        self._resubscribe()
        if entities:
            await self._call(room_id, "scene", "apply", {"entities": entities})

    async def _call(
        self, room_id: str, domain: str, service: str, data: dict[str, Any]
    ) -> None:
        """One Perch service call, in its own context so its changes are not
        mistaken for a hand change."""
        context = Context()
        self._contexts[context.id] = None
        self._last_call[room_id] = time.monotonic()
        while len(self._contexts) > _MAX_CONTEXTS:
            self._contexts.popitem(last=False)
        try:
            await self.hass.services.async_call(
                domain, service, data, blocking=True, context=context
            )
        except HomeAssistantError as err:
            _LOGGER.warning("Perch could not call %s.%s: %s", domain, service, err)
        self._last_call[room_id] = time.monotonic()

    # --- hand-change tracking ----------------------------------------------

    def note_user_context(self, context_id: str) -> None:
        """A TV's own light or scene command: its changes are always hand
        changes, grace window or not."""
        self._user_contexts[context_id] = None
        while len(self._user_contexts) > _MAX_CONTEXTS:
            self._user_contexts.popitem(last=False)

    def _resubscribe(self) -> None:
        wanted = frozenset(e for room in self._rooms.values() for e in room["snapshot"])
        if wanted == self._watched and (self._unsub or not wanted):
            return
        if self._unsub:
            self._unsub()
            self._unsub = None
        self._watched = wanted
        if wanted and not self._unloaded:
            self._unsub = async_track_state_change_event(
                self.hass, list(wanted), self._state_changed
            )

    @callback
    def _state_changed(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        new, old = event.data["new_state"], event.data["old_state"]
        if new is None or old is None or new.state in _GONE or old.state in _GONE:
            return
        found = next(
            ((i, r) for i, r in self._rooms.items() if entity_id in r["snapshot"]), None
        )
        if found is None:
            return
        room_id, room = found
        value = _value(new)
        last = self._last_call.get(room_id)
        in_grace = last is not None and time.monotonic() - last <= HAND_CHANGE_GRACE
        by_user = event.context.id in self._user_contexts
        if event.context.id in self._contexts or (in_grace and not by_user):
            room["perch_values"][entity_id] = value
            self._save_soon()
            return
        baseline = room["perch_values"].get(entity_id)
        if baseline is None:
            snap = room["snapshot"][entity_id]
            baseline = {"on": snap["state"] == "on", "brightness": snap.get("brightness")}
        if not _same(value, baseline) and entity_id not in room["hand_changed"]:
            room["hand_changed"].append(entity_id)
            self._save_soon()

    @callback
    def _save_soon(self) -> None:
        if not self._unloaded:
            self._store.async_delay_save(self._data, 1)
