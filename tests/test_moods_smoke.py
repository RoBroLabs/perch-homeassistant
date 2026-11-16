"""Smoke tests: dim on play, restore on stop, restore across a reload."""

from __future__ import annotations

from .fakes import setup_lights
from .test_endpoint import PLAYBACK, client, cmd, connect, world  # noqa: F401


async def _playback(ws, state, cid=9):
    data = await cmd(
        ws, cid, "perch/playback", state=state, media="film", lighting=True
    )
    assert data["result"] == {"applied": True, "reason": None}


async def test_film_dims_and_stop_restores(hass, client, world):  # noqa: F811
    lights = await setup_lights(hass)
    ws = await connect(client, world.tv_a)
    await _playback(ws, "playing")
    assert lights.level(world.light_a) == ("on", 26)  # 10 %
    await _playback(ws, "stopped", 10)
    assert lights.level(world.light_a) == ("on", 128)


async def test_restore_survives_a_reload(hass, client, world):  # noqa: F811
    lights = await setup_lights(hass)
    ws = await connect(client, world.tv_a)
    await _playback(ws, "playing")
    assert lights.level(world.light_a) == ("on", 26)
    assert await hass.config_entries.async_reload(world.entry.entry_id)
    await hass.async_block_till_done()
    ws = await connect(client, world.tv_a)
    await _playback(ws, "stopped", 11)
    assert lights.level(world.light_a) == ("on", 128)


async def test_hand_changed_light_is_left_alone(hass, client, world, freezer):  # noqa: F811
    lights = await setup_lights(hass)
    ws = await connect(client, world.tv_a)
    await _playback(ws, "playing")
    freezer.tick(30)  # past the hand-change grace window
    lights.set(world.light_a, True, 200)
    await hass.async_block_till_done()
    await _playback(ws, "stopped", 10)
    assert lights.level(world.light_a) == ("on", 200)
