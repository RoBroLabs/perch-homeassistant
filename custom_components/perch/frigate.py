"""Frigate events through the official Frigate integration, and the
companion's own short-lived media URLs.

The TV never sees Frigate: no Frigate URL, no credential, no notification
proxy. It gets `/api/perch/media/<token>/<kind>` paths that the companion
serves itself, from the Frigate client the integration already holds. Every
Frigate call is guarded: a missing, unsupported or failing Frigate is
`unavailable`, never a crash.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
import re
import secrets
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .const import (
    FRIGATE_DOMAIN,
    FRIGATE_SUPPORTED_MAJOR,
    FRIGATE_TESTED_VERSIONS,
    MEDIA_LIFETIME,
)

_LOGGER = logging.getLogger(__name__)

EVENTS_CAP = 100
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class FrigateUnavailable(Exception):
    """Frigate is missing, unsupported or failed; becomes `unavailable`."""


@dataclass(frozen=True)
class MediaGrant:
    entry_id: str
    event_id: str
    kind: str
    tv_id: str
    camera_id: str
    expires_at: datetime


class MediaTokens:
    """Short-lived media tokens, in memory only."""

    def __init__(self) -> None:
        self._grants: dict[str, MediaGrant] = {}

    def prune(self) -> None:
        now = dt_util.utcnow()
        for token in [t for t, g in self._grants.items() if g.expires_at <= now]:
            del self._grants[token]

    def issue(
        self, entry_id: str, event_id: str, kind: str, tv_id: str, camera_id: str
    ) -> tuple[str, datetime]:
        self.prune()
        expires = dt_util.utcnow() + MEDIA_LIFETIME
        token = secrets.token_urlsafe(32)
        self._grants[token] = MediaGrant(
            entry_id, event_id, kind, tv_id, camera_id, expires
        )
        return token, expires

    def lookup(self, token: str, kind: str) -> MediaGrant | None:
        grant = self._grants.get(token)
        if grant is None:
            return None
        if grant.expires_at <= dt_util.utcnow():
            self._grants.pop(token, None)
            return None
        return grant if grant.kind == kind else None


def _clients(hass: HomeAssistant) -> dict[str, Any]:
    """Frigate config entry id -> API client, for entries that have one."""
    data = hass.data.get(FRIGATE_DOMAIN)
    if not isinstance(data, dict):
        return {}
    return {
        entry_id: value["client"]
        for entry_id, value in data.items()
        if isinstance(value, dict) and "client" in value
    }


async def async_check_version(hass: HomeAssistant, warned: set[str]) -> None:
    """Raise FrigateUnavailable unless a supported Frigate is installed."""
    try:
        integration = await async_get_integration(hass, FRIGATE_DOMAIN)
        version = str(integration.version)
        major = int(version.split(".")[0])
    except Exception:  # noqa: BLE001 - not installed or unreadable
        raise FrigateUnavailable from None
    if major != FRIGATE_SUPPORTED_MAJOR:
        if version not in warned:
            warned.add(version)
            _LOGGER.warning(
                "Frigate integration %s is not supported (tested: %s)",
                version,
                ", ".join(FRIGATE_TESTED_VERSIONS),
            )
        raise FrigateUnavailable


def camera_names(
    hass: HomeAssistant, camera_ids: frozenset[str] | set[str]
) -> dict[str, tuple[str, str]]:
    """HA camera entity id -> (Frigate entry id, Frigate camera name), for the
    given entities that the Frigate integration owns."""
    reg = er.async_get(hass)
    clients = _clients(hass)
    mapping: dict[str, tuple[str, str]] = {}
    for camera_id in camera_ids:
        ent = reg.async_get(camera_id)
        if ent is None or ent.platform != FRIGATE_DOMAIN or ent.domain != "camera":
            continue
        for entry_id in clients:
            prefix = f"{entry_id}:camera:"
            if ent.unique_id.startswith(prefix) and len(ent.unique_id) > len(prefix):
                mapping[camera_id] = (entry_id, ent.unique_id[len(prefix) :])
                break
    return mapping


def day_bounds(day: str) -> tuple[int, int]:
    """Midnight to midnight of `day` in Home Assistant's time zone, as epochs."""
    start = dt_util.start_of_local_day(date.fromisoformat(day))
    end = dt_util.start_of_local_day(date.fromisoformat(day) + timedelta(days=1))
    return int(start.timestamp()), int(end.timestamp())


async def async_list_events(
    hass: HomeAssistant,
    camera_ids: frozenset[str] | set[str],
    day: str,
    warned: set[str],
) -> list[tuple[str, str, str, dict[str, Any]]]:
    """Events for the day, newest first, at most EVENTS_CAP, as tuples of
    (frigate entry id, HA camera id, event id, raw event)."""
    await async_check_version(hass, warned)
    clients = _clients(hass)
    if not clients:
        raise FrigateUnavailable
    mapping = camera_names(hass, camera_ids)
    after, before = day_bounds(day)
    found: list[tuple[float, str, str, str, dict[str, Any]]] = []
    for entry_id, client in clients.items():
        by_name = {
            name: camera_id
            for camera_id, (eid, name) in mapping.items()
            if eid == entry_id
        }
        if not by_name:
            continue
        try:
            raw = await client.async_get_events(
                cameras=sorted(by_name),
                after=after,
                before=before,
                limit=EVENTS_CAP,
                decode_json=True,
            )
        except Exception:  # noqa: BLE001 - any Frigate failure is "unavailable"
            _LOGGER.debug("Frigate events call failed", exc_info=True)
            raise FrigateUnavailable from None
        if not isinstance(raw, list):
            raise FrigateUnavailable
        for event in raw:
            if not isinstance(event, dict):
                continue
            event_id = event.get("id")
            camera_id = by_name.get(event.get("camera"))
            start = event.get("start_time")
            if (
                not isinstance(event_id, str)
                or not _SAFE_ID.match(event_id)
                or camera_id is None
                or isinstance(start, bool)
                or not isinstance(start, (int, float))
            ):
                continue
            found.append((float(start), entry_id, camera_id, event_id, event))
    found.sort(key=lambda item: item[0], reverse=True)
    return [(e, c, i, ev) for _, e, c, i, ev in found[:EVENTS_CAP]]


def utc_timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, dt_util.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def media_target(
    hass: HomeAssistant, entry_id: str, event_id: str, kind: str
) -> tuple[str, Any] | None:
    """The upstream media URL and the Frigate client, or None."""
    client = _clients(hass).get(entry_id)
    entry: ConfigEntry | None = hass.config_entries.async_get_entry(entry_id)
    if client is None or entry is None or not _SAFE_ID.match(event_id):
        return None
    base = str(entry.data.get("url", "")).rstrip("/")
    if not base:
        return None
    leaf = "snapshot.jpg" if kind == "snapshot" else "clip.mp4"
    return f"{base}/api/events/{event_id}/{leaf}", client
