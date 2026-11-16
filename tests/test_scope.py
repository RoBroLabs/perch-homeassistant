"""Scope and manifest: what counts as curated."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.perch.const import DOMAIN
from custom_components.perch.scope import build_manifest, build_scope, is_allowed

from .schema import validate


def _sub(tv_id, name, room=None, rooms=()):
    data = {"tv_id": tv_id, "public_key": "PK", "credential_hash": "H"}
    if room:
        data["room_id"] = room
        data["rooms"] = [room, *rooms]
    return ConfigSubentryData(
        data=data, subentry_type="tv", title=name, unique_id=tv_id
    )


async def _setup(hass, options=None, subs=()):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={},
        options=options or {},
        subentries_data=list(subs),
    )
    entry.add_to_hass(hass)
    return entry


def _ent(hass, domain, key, *, area=None, device=None, name=None, state="on", **attrs):
    reg = er.async_get(hass)
    # HA 2026.10+ refuses an area on an entity with no name of its own.
    e = reg.async_get_or_create(
        domain,
        "test",
        key,
        suggested_object_id=key,
        device_id=device,
        original_name=name or key.title(),
    )
    if area:
        reg.async_update_entity(e.entity_id, area_id=area)
    hass.states.async_set(
        e.entity_id, state, {"friendly_name": name or key.title(), **attrs}
    )
    return e.entity_id


async def _world(hass):
    areas = ar.async_get(hass)
    lounge = areas.async_create("Lounge").id
    kitchen = areas.async_create("Kitchen").id
    bedroom = areas.async_create("Bedroom").id
    return lounge, kitchen, bedroom


async def test_lights_by_entity_area_device_area_and_never_switch(hass):
    lounge, kitchen, _ = await _world(hass)
    entry = await _setup(hass, subs=[_sub("tv_a", "Lounge TV", lounge)])
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=_add(hass), identifiers={("t", "d")}
    )
    dr.async_get(hass).async_update_device(dev.id, area_id=lounge)
    by_entity = _ent(hass, "light", "lamp", area=lounge)
    by_device = _ent(hass, "light", "strip", device=dev.id)
    # Entity area wins over device area.
    moved = _ent(hass, "light", "moved", device=dev.id, area=kitchen)
    elsewhere = _ent(hass, "light", "pantry", area=kitchen)
    switch = _ent(hass, "switch", "plug", area=lounge)
    scope = build_scope(hass, entry, "tv_a")
    assert scope.lights == {by_entity, by_device}
    assert moved not in scope.lights and elsewhere not in scope.lights
    assert not is_allowed(scope, switch)
    assert is_allowed(scope, by_entity)


def _add(hass):
    holder = MockConfigEntry(domain="t")
    holder.add_to_hass(hass)
    return holder.entry_id


async def test_manifest_matches_selection_and_schema(hass):
    lounge, kitchen, _ = await _world(hass)
    cam = _ent(hass, "camera", "front", area=kitchen, name="Front door")
    cam2 = _ent(hass, "camera", "garden", name="Garden")
    bell = _ent(hass, "binary_sensor", "bell")
    mn = _ent(hass, "scene", "mn", name="Movie night")
    other = _ent(hass, "scene", "bright", name="Bright")
    options = {
        "cameras": [cam, cam2],
        "doorbell": bell,
        "scenes": [mn, other],
        "doorbell_camera": cam,
        "movie_night_scene": mn,
    }
    entry = await _setup(
        hass, options, [_sub("tv_a", "Lounge TV", lounge, [kitchen])]
    )
    dim = _ent(hass, "light", "lamp", area=lounge, name="Lamp",
               supported_color_modes=["brightness"])
    onoff = _ent(hass, "light", "plain", area=kitchen, name="Plain",
                 supported_color_modes=["onoff"])
    scope = build_scope(hass, entry, "tv_a")
    assert scope.rooms == (lounge, kitchen)
    manifest = build_manifest(hass, entry, scope)
    validate("ManifestResult", manifest)
    assert manifest == {
        "tv_id": "tv_a",
        "tv_name": "Lounge TV",
        "room": {"id": lounge, "name": "Lounge"},
        "rooms": [
            {"id": lounge, "name": "Lounge"},
            {"id": kitchen, "name": "Kitchen"},
        ],
        "lights": sorted(
            [
                {"id": dim, "name": "Lamp", "room_id": lounge, "dimmable": True},
                {"id": onoff, "name": "Plain", "room_id": kitchen, "dimmable": False},
            ],
            key=lambda x: x["id"],
        ),
        "cameras": sorted(
            [
                {"id": cam, "name": "Front door", "room_id": kitchen},
                {"id": cam2, "name": "Garden", "room_id": None},
            ],
            key=lambda x: x["id"],
        ),
        "doorbell": {"camera_id": cam},
        "scenes": sorted(
            [
                {"id": mn, "name": "Movie night", "purpose": "movie_night"},
                {"id": other, "name": "Bright", "purpose": None},
            ],
            key=lambda x: x["id"],
        ),
        "let_in_available": False,
    }
    assert is_allowed(scope, cam) and is_allowed(scope, bell) and is_allowed(scope, mn)
    assert not is_allowed(scope, "camera.unselected")


async def test_doorbell_without_camera_and_purpose_only_from_pickers(hass):
    cam = _ent(hass, "camera", "front")
    bell = _ent(hass, "binary_sensor", "bell")
    mn = _ent(hass, "scene", "mn", name="Movie night")
    pz = _ent(hass, "scene", "pz", name="Paused")
    outside = _ent(hass, "scene", "out", name="Elsewhere")
    options = {
        "cameras": [cam],
        "doorbell": bell,
        "scenes": [mn, pz],
        # Not picked: names alone give nothing. A stale pick outside the
        # selection and a camera outside the selection are ignored too.
        "paused_scene": outside,
        "doorbell_camera": "camera.gone",
    }
    entry = await _setup(hass, options, [_sub("tv_a", "Lounge TV")])
    manifest = build_manifest(hass, entry, build_scope(hass, entry, "tv_a"))
    validate("ManifestResult", manifest)
    assert manifest["doorbell"] == {"camera_id": None}
    assert {s["id"]: s["purpose"] for s in manifest["scenes"]} == {mn: None, pz: None}


async def test_camera_ignored_without_trigger(hass):
    cam = _ent(hass, "camera", "front")
    entry = await _setup(
        hass, {"cameras": [cam], "doorbell_camera": cam}, [_sub("tv_a", "TV")]
    )
    manifest = build_manifest(hass, entry, build_scope(hass, entry, "tv_a"))
    assert manifest["doorbell"] is None


async def test_empty_selection_manifest_validates(hass):
    entry = await _setup(hass, subs=[_sub("tv_a", "New TV")])
    scope = build_scope(hass, entry, "tv_a")
    assert scope.room_id is None and scope.rooms == ()
    manifest = build_manifest(hass, entry, scope)
    validate("ManifestResult", manifest)
    # A TV with no room yet sends room null, never a sentinel.
    assert manifest["room"] is None and manifest["rooms"] == []
    assert manifest["doorbell"] is None
    assert manifest["lights"] == manifest["cameras"] == manifest["scenes"] == []


async def test_two_tvs_see_only_their_own_lights(hass):
    lounge, kitchen, bedroom = await _world(hass)
    entry = await _setup(
        hass,
        subs=[
            _sub("tv_a", "Lounge TV", lounge),
            _sub("tv_b", "Bedroom TV", bedroom),
        ],
    )
    la = _ent(hass, "light", "lounge_lamp", area=lounge)
    lb = _ent(hass, "light", "bed_lamp", area=bedroom)
    lk = _ent(hass, "light", "kitchen_lamp", area=kitchen)
    a = build_scope(hass, entry, "tv_a")
    b = build_scope(hass, entry, "tv_b")
    assert a.lights == {la} and b.lights == {lb}
    assert not is_allowed(a, lb) and not is_allowed(b, la)
    assert not is_allowed(a, lk) and not is_allowed(b, lk)
    assert {x["id"] for x in build_manifest(hass, entry, b)["lights"]} == {lb}


async def test_unknown_tv_gives_none(hass):
    entry = await _setup(hass, subs=[_sub("tv_a", "Lounge TV")])
    assert build_scope(hass, entry, "tv_zzz") is None
    assert build_scope(hass, entry, "") is None
