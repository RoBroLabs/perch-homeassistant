"""Options flow and the per-TV reconfigure step."""

from __future__ import annotations

import pytest
import voluptuous as vol
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr

from custom_components.perch.const import DOMAIN


async def _entry(hass, **kw):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={}, **kw)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _adv(attempts=5, cloud=False):
    return {"pairing_attempts": attempts, "allow_cloud": cloud}


async def test_options_round_trip(hass):
    entry = await _entry(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "cameras": ["camera.front", "camera.back"],
            "doorbell": "binary_sensor.bell",
            "scenes": ["scene.movie_night"],
            "restore_timeout": 20,
            "advanced": _adv(7, True),
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "details"
    result = await hass.config_entries.options.async_configure(flow["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        "cameras": ["camera.front", "camera.back"],
        "doorbell": "binary_sensor.bell",
        "scenes": ["scene.movie_night"],
        "restore_timeout": 20,
        "pairing_attempts": 7,
        "allow_cloud": True,
        "notify_services": [],
        "notify_request_ready": False,
        "notify_include_title": False,
    }
    assert type(entry.options["restore_timeout"]) is int
    assert type(entry.options["pairing_attempts"]) is int


async def test_options_defaults_and_reopen_clears_doorbell(hass):
    entry = await _entry(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"advanced": {}, "doorbell": "binary_sensor.bell"}
    )
    assert entry.options["restore_timeout"] == 15
    assert entry.options["pairing_attempts"] == 5
    assert entry.options["allow_cloud"] is False
    assert entry.options["cameras"] == []
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        flow["flow_id"], {"advanced": {}}
    )
    assert "doorbell" not in entry.options


@pytest.mark.parametrize(
    ("top", "advanced"),
    [
        ({"restore_timeout": 4}, {}),
        ({"restore_timeout": 61}, {}),
        ({}, {"pairing_attempts": 2}),
        ({}, {"pairing_attempts": 11}),
    ],
)
async def test_options_bounds_refused(hass, top, advanced):
    entry = await _entry(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            flow["flow_id"], {**top, "advanced": advanced}
        )


@pytest.mark.parametrize(("t", "a"), [(5, 3), (60, 10)])
async def test_options_bounds_accepted(hass, t, a):
    entry = await _entry(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"restore_timeout": t, "advanced": _adv(a)}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert (entry.options["restore_timeout"], entry.options["pairing_attempts"]) == (t, a)


async def test_options_reject_wrong_domain(hass):
    entry = await _entry(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            flow["flow_id"], {"cameras": ["light.kitchen"], "advanced": {}}
        )


async def test_reconfigure_stores_rooms_and_keeps_keys(hass):
    sub = ConfigSubentryData(
        data={"tv_id": "tv_a", "public_key": "PK", "credential_hash": "H"},
        subentry_type="tv",
        title="Lounge",
        unique_id="tv_a",
    )
    entry = await _entry(hass, subentries_data=[sub])
    areas = ar.async_get(hass)
    lounge = areas.async_create("Lounge")
    kitchen = areas.async_create("Kitchen")
    sid = next(iter(entry.subentries))
    flow = await entry.start_subentry_reconfigure_flow(hass, sid)
    assert flow["type"] is FlowResultType.FORM
    assert flow["step_id"] == "reconfigure"
    result = await hass.config_entries.subentries.async_configure(
        flow["flow_id"],
        {"room_id": lounge.id, "rooms": [kitchen.id, lounge.id]},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    data = dict(entry.subentries[sid].data)
    assert data == {
        "tv_id": "tv_a",
        "public_key": "PK",
        "credential_hash": "H",
        "room_id": lounge.id,
        "rooms": [lounge.id, kitchen.id],
    }
    # The device survives the update.
    reg = dr.async_get(hass)
    assert reg.async_get_device_by_identifier((DOMAIN, "tv_a"), entry.entry_id)


async def test_reconfigure_requires_room(hass):
    sub = ConfigSubentryData(
        data={"tv_id": "tv_a", "public_key": "PK", "credential_hash": "H"},
        subentry_type="tv",
        title="Lounge",
        unique_id="tv_a",
    )
    entry = await _entry(hass, subentries_data=[sub])
    sid = next(iter(entry.subentries))
    flow = await entry.start_subentry_reconfigure_flow(hass, sid)
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            flow["flow_id"], {"rooms": []}
        )


async def test_options_change_keeps_device(hass):
    sub = ConfigSubentryData(
        data={"tv_id": "tv_a", "public_key": "PK", "credential_hash": "H"},
        subentry_type="tv",
        title="Lounge",
        unique_id="tv_a",
    )
    entry = await _entry(hass, subentries_data=[sub])
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        flow["flow_id"], {"advanced": {}}
    )
    await hass.async_block_till_done()
    assert dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "tv_a"), entry.entry_id
    )
    assert len(entry.subentries) == 1


STEP1 = {
    "cameras": ["camera.front", "camera.back"],
    "doorbell": "binary_sensor.bell",
    "scenes": ["scene.movie_night", "scene.paused", "scene.bright"],
    "advanced": {},
}


async def _details(hass, entry, step1=STEP1):
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], step1
    )
    assert result["step_id"] == "details"
    return flow, result


async def test_details_stores_pickers(hass):
    entry = await _entry(hass)
    flow, _ = await _details(hass, entry)
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "doorbell_camera": "camera.back",
            "movie_night_scene": "scene.movie_night",
            "paused_scene": "scene.paused",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["doorbell_camera"] == "camera.back"
    assert entry.options["movie_night_scene"] == "scene.movie_night"
    assert entry.options["paused_scene"] == "scene.paused"


async def test_details_same_scene_for_both_refused(hass):
    entry = await _entry(hass)
    flow, _ = await _details(hass, entry)
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {"movie_night_scene": "scene.bright", "paused_scene": "scene.bright"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "same_scene"}
    assert "movie_night_scene" not in entry.options
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {"movie_night_scene": "scene.bright", "paused_scene": "scene.paused"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_details_choices_limited_to_step_one(hass):
    entry = await _entry(hass)
    flow, result = await _details(hass, entry)
    for key, bad in (
        ("doorbell_camera", "camera.other"),
        ("movie_night_scene", "scene.other"),
        ("paused_scene", "scene.other"),
    ):
        with pytest.raises(InvalidData):
            await hass.config_entries.options.async_configure(
                flow["flow_id"], {key: bad}
            )
    schema = result["data_schema"].schema
    offered = {
        str(k): {o["value"] for o in v.config["options"]} for k, v in schema.items()
    }
    assert offered == {
        "doorbell_camera": set(STEP1["cameras"]),
        "movie_night_scene": set(STEP1["scenes"]),
        "paused_scene": set(STEP1["scenes"]),
    }


async def test_details_name_only_preselects(hass):
    hass.states.async_set("scene.movie_night", "scening", {"friendly_name": "Movie night"})
    entry = await _entry(hass)
    flow, result = await _details(hass, entry)
    suggested = {
        str(k): (k.description or {}).get("suggested_value")
        for k in result["data_schema"].schema
    }
    assert suggested["movie_night_scene"] == "scene.movie_night"
    assert suggested["paused_scene"] is None
    # Submitting without choosing stores nothing.
    await hass.config_entries.options.async_configure(flow["flow_id"], {})
    assert "movie_night_scene" not in entry.options


async def test_details_skipped_without_cameras_or_scenes(hass):
    entry = await _entry(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"doorbell": "binary_sensor.bell", "advanced": {}}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
