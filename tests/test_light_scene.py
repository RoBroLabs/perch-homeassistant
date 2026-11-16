"""`perch/light` and `perch/scene`: scoped per TV, run under a context the
mood engine does not own."""

from __future__ import annotations

from custom_components.perch.runtime import get_runtime
from custom_components.perch.scope import role_may_control_lights, role_may_view_events

from .fakes import setup_lights
from .test_endpoint import client, cmd, connect, world  # noqa: F401

SCENE = "scene.movie_night"


async def _setup(hass, world):
    fake = await setup_lights(
        hass, [{"name": "Movie night", "entities": {world.light_a: {"state": "on", "brightness": 30}}}]
    )
    hass.config_entries.async_update_entry(
        world.entry,
        options={"cameras": [world.camera], "doorbell": "", "scenes": [SCENE, "scene.other"]},
    )
    await hass.async_block_till_done()
    return fake


async def test_light_on_off_brightness(hass, client, world):
    fake = await _setup(hass, world)
    ws = await connect(client, world.tv_a)
    data = await cmd(ws, 1, "perch/light", light_id=world.light_a, on=True, brightness=25)
    assert data["result"] == {}
    assert fake.calls[-1] == ("turn_on", world.light_a, {"brightness_pct": 25})
    await cmd(ws, 2, "perch/light", light_id=world.light_a, brightness=60)
    assert fake.level(world.light_a) == ("on", round(60 * 255 / 100))
    await cmd(ws, 3, "perch/light", light_id=world.light_a, on=True)
    assert fake.calls[-1] == ("turn_on", world.light_a, {})
    await cmd(ws, 4, "perch/light", light_id=world.light_a, on=False, brightness=50)
    assert fake.calls[-1][0] == "turn_off"
    assert fake.level(world.light_a)[0] == "off"


async def test_light_scoped_per_tv(hass, client, world):
    fake = await _setup(hass, world)
    extra = hass.states.async_set("light.not_curated", "on")
    ws = await connect(client, world.tv_a)
    for light in (world.light_b, "light.not_curated"):
        data = await cmd(ws, 1, "perch/light", light_id=light, on=False)
        assert data["error"]["code"] == "not_allowed"
    assert not fake.calls
    bad = await cmd(ws, 2, "perch/light", light_id=world.light_a)
    assert bad["error"]["code"] == "invalid_format"


async def test_scene_allowed_and_refused(hass, client, world):
    fake = await _setup(hass, world)
    ws = await connect(client, world.tv_a)
    seen = []
    # Recorded here: tests/fakes.py builds its scene config with a wrong key.
    hass.services.async_register(
        "scene", "turn_on", lambda call: seen.append((call.data["entity_id"], call.context))
    )
    data = await cmd(ws, 1, "perch/scene", scene_id=SCENE)
    assert data["result"] == {}
    assert [e for e, _ in seen] == [SCENE]
    assert get_runtime(hass).moods is not None
    assert seen[0][1].id not in get_runtime(hass).moods._contexts
    data = await cmd(ws, 2, "perch/scene", scene_id="scene.unlisted")
    assert data["error"]["code"] == "not_allowed"


async def test_light_during_film_is_a_hand_change(hass, client, world):
    fake = await _setup(hass, world)
    fake.set(world.light_a, True, 200)
    ws = await connect(client, world.tv_a)
    playing = await cmd(
        ws, 1, "perch/playback", state="playing", media="film", lighting=True
    )
    assert playing["result"]["applied"] is True
    await hass.async_block_till_done()
    assert fake.level(world.light_a) == ("on", round(10 * 255 / 100))
    await cmd(ws, 2, "perch/light", light_id=world.light_a, brightness=80)
    await hass.async_block_till_done()
    await cmd(ws, 3, "perch/playback", state="stopped", media="film", lighting=True)
    await hass.async_block_till_done()
    assert fake.level(world.light_a) == ("on", round(80 * 255 / 100))


def test_role_helpers():
    assert role_may_control_lights("adult") and role_may_control_lights("teen")
    assert not role_may_control_lights("kid")
    assert role_may_view_events("adult") and role_may_view_events("teen")
    assert not role_may_view_events("kid")
