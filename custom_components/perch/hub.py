"""Pushes to subscribed sockets: doorbell, states and manifest.

Subscriptions live per socket and die with it. Every push is resolved from the
TV bound to the socket, so a TV only ever hears about its own scope.
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from aiohttp import web

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .commands import TOPICS, light_state
from .const import CONF_DOORBELL
from .scope import build_scope

_LOGGER = logging.getLogger(__name__)
_IGNORED = ("unavailable", "unknown")


@callback
def _is_light(data: Any) -> bool:
    return data["entity_id"].startswith("light.")


class PushHub:
    """Subscriptions and the pushes that feed them."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.last_ring: datetime | None = None
        # socket -> (tv_id, {topic: id of the newest subscribe command})
        self._subs: dict[web.WebSocketResponse, tuple[str, dict[str, int]]] = {}
        self._doorbell: str | None = None
        self._unsub_doorbell = None
        self._unsub_lights = None

    @callback
    def async_start(self) -> None:
        self._unsub_lights = self.hass.bus.async_listen(
            EVENT_STATE_CHANGED,
            self._light_changed,
            event_filter=_is_light,
        )
        self.rearm()

    @callback
    def async_stop(self) -> None:
        if self._unsub_lights:
            self._unsub_lights()
            self._unsub_lights = None
        self._disarm()
        self._subs.clear()

    def _disarm(self) -> None:
        if self._unsub_doorbell:
            self._unsub_doorbell()
            self._unsub_doorbell = None
        self._doorbell = None

    @callback
    def rearm(self) -> None:
        """Watch the current doorbell option entity (options may have changed)."""
        wanted = self.entry.options.get(CONF_DOORBELL) or None
        if wanted == self._doorbell and (self._unsub_doorbell or not wanted):
            return
        self._disarm()
        if wanted:
            self._doorbell = wanted
            self._unsub_doorbell = async_track_state_change_event(
                self.hass, [wanted], self._doorbell_changed
            )

    @callback
    def entry_changed(self) -> None:
        """Options or subentries changed: re-arm and tell every subscriber."""
        self.rearm()
        self._push_all("manifest", lambda tv_id: {})

    @callback
    def subscribe(
        self, ws: web.WebSocketResponse, tv_id: str, topics: list[str], cid: int
    ) -> list[str]:
        _, mine = self._subs.setdefault(ws, (tv_id, {}))
        for topic in topics:
            mine[topic] = cid
        return sorted(mine)

    @callback
    def drop(self, ws: web.WebSocketResponse) -> None:
        self._subs.pop(ws, None)

    @callback
    def _doorbell_changed(self, event: Event) -> None:
        new, old = event.data["new_state"], event.data["old_state"]
        if new is None or old is None:
            return
        if new.state in _IGNORED or old.state in _IGNORED:
            return
        if new.entity_id.startswith("event."):
            pressed = new.state != old.state
        else:
            pressed = old.state == "off" and new.state == "on"
        if not pressed:
            return
        now = dt_util.utcnow()
        self.last_ring = now
        rang_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        entity_id = new.entity_id

        def data(tv_id: str) -> dict[str, Any] | None:
            scope = build_scope(self.hass, self.entry, tv_id)
            if scope is None or scope.doorbell != entity_id:
                return None
            return {"camera_id": scope.doorbell_camera, "rang_at": rang_at}

        self._push_all("doorbell", data)

    @callback
    def _light_changed(self, event: Event) -> None:
        entity_id = event.data["entity_id"]

        def data(tv_id: str) -> dict[str, Any] | None:
            scope = build_scope(self.hass, self.entry, tv_id)
            if scope is None or entity_id not in scope.lights:
                return None
            return {"lights": [light_state(self.hass, entity_id)]}

        self._push_all("states", data)

    # `events` is accepted but never pushed: the app reads
    # `perch/events` on demand, so a push is not needed.

    def _push_all(self, topic: str, data_for) -> None:
        for ws, (tv_id, topics) in list(self._subs.items()):
            cid = topics.get(topic)
            if cid is None:
                continue
            data = data_for(tv_id)
            if data is None:
                continue
            frame = {"id": cid, "type": "event", "event": {"topic": topic, "data": data}}
            self.hass.async_create_background_task(
                self._send(ws, frame), f"perch push {topic}"
            )

    async def _send(self, ws: web.WebSocketResponse, frame: dict[str, Any]) -> None:
        if ws.closed:
            return
        try:
            await ws.send_json(frame)
        except (ConnectionError, RuntimeError):
            _LOGGER.debug("Push to a closing socket dropped")
