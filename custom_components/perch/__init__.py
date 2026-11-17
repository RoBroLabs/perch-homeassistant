"""Perch for Home Assistant: the companion the Perch Apple TV app pairs with."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .const import CONF_ALLOW_CLOUD, CONF_ROOM_ID, CONF_TV_ID, DOMAIN, SUBENTRY_TV
from .access import Origin
from .hub import PushHub
from .moods import MoodEngine
from .runtime import PerchRuntime, get_runtime
from .endpoint import close_all_sockets, close_cloud_sockets, revoke_sockets
from .views import PairStatusView, PairView, WsView

_LOGGER = logging.getLogger(__name__)
_VIEWS_KEY = f"{DOMAIN}_views_registered"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Perch from its config entry."""
    if not hass.data.get(_VIEWS_KEY):
        hass.http.register_view(PairView())
        hass.http.register_view(PairStatusView())
        hass.http.register_view(WsView())
        hass.data[_VIEWS_KEY] = True
    runtime = PerchRuntime(entry=entry)
    runtime.moods = MoodEngine(hass, entry)
    await runtime.moods.async_load()
    runtime.hub = PushHub(hass, entry)
    runtime.hub.async_start()
    hass.data[DOMAIN] = runtime
    _reconcile(hass, entry)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Perch; the views stay registered but answer not_found."""
    runtime = get_runtime(hass)
    if runtime is not None:
        runtime.pairing.clear()
        hass.data.pop(DOMAIN, None)
        if runtime.hub is not None:
            runtime.hub.async_stop()
        await close_all_sockets(runtime)
        if runtime.moods is not None:
            await runtime.moods.async_unload()
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    _reconcile(hass, entry)


@callback
def _reconcile(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """One device per paired TV; a device whose TV is gone is removed."""
    dev_reg = dr.async_get(hass)
    present: set[str] = set()
    rooms: dict[str, str | None] = {}
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TV:
            continue
        tv_id = subentry.data[CONF_TV_ID]
        present.add(tv_id)
        rooms[tv_id] = subentry.data.get(CONF_ROOM_ID) or None
        dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            config_subentry_id=subentry.subentry_id,
            identifiers={(DOMAIN, tv_id)},
            name=subentry.title,
            manufacturer="Perch",
            model="Apple TV",
        )
    # A TV whose subentry is gone is revoked: its open sockets are told and closed.
    runtime = get_runtime(hass)
    if runtime is not None:
        if runtime.hub is not None:
            runtime.hub.entry_changed()
        if runtime.moods is not None:
            for tv_id in runtime.moods.known_tvs():
                keep = rooms.get(tv_id)
                if tv_id in present and runtime.moods.room_of(tv_id) in (None, keep):
                    continue
                hass.async_create_background_task(
                    runtime.moods.async_release_tv(
                        tv_id,
                        keep_room=keep if tv_id in present else None,
                        removed=tv_id not in present,
                    ),
                    f"perch release {tv_id}",
                )
        for tv_id in set(runtime.sockets) - present:
            hass.async_create_background_task(
                revoke_sockets(runtime, tv_id), f"perch revoke {tv_id}"
            )
        if not entry.options.get(CONF_ALLOW_CLOUD, False) and any(
            origin is Origin.CLOUD for origin in runtime.origins.values()
        ):
            hass.async_create_background_task(
                close_cloud_sockets(runtime), "perch close cloud sockets"
            )
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        tv_ids = {i[1] for i in device.identifiers if i[0] == DOMAIN}
        if tv_ids and not tv_ids & present:
            dev_reg.async_remove_device(device.id)
            _LOGGER.info("Removed Perch TV %s", device.name)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Removing a TV's device unpairs it: its subentry (and hash) goes too."""
    tv_ids = {i[1] for i in device.identifiers if i[0] == DOMAIN}
    for subentry in list(entry.subentries.values()):
        if subentry.subentry_type == SUBENTRY_TV and subentry.data[CONF_TV_ID] in tv_ids:
            hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
    return True
