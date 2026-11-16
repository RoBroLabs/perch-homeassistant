"""The mood engine through the TV WebSocket.

Play, pause and stop move the lights; a restart mid-film still restores; a
light a person changed is left alone; a room with two TVs restores only when
both have stopped. Lights are fakes (see fakes.py); `scene.apply` is real.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import MappingProxyType

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.config_entries import ConfigSubentry
from homeassistant.util import dt as dt_util

from custom_components.perch.runtime import get_runtime
from custom_components.perch.scope import build_scope

from .fakes import setup_lights
from .test_endpoint import Tv, _ent, client, cmd, connect, world  # noqa: F401

# What the fake lights write for brightness_pct.
PCT10, PCT35, PCT50 = 26, 89, 128
HS = [30.0, 60.0]


async def play(ws, state, media="film", lighting=True, cid=9):
    data = await cmd(
        ws, cid, "perch/playback", state=state, media=media, lighting=lighting
    )
    assert data["success"], data
    return data["result"]


APPLIED = {"applied": True, "reason": None}


@pytest.fixture
async def lights(hass, world):  # noqa: F811
    """Fake lights plus two scenes, and a Lounge with four lights:
    lamp 255 with a colour, bulb 200, dimlamp 20 (8 %), spare off."""
    fake = await setup_lights(
        hass,
        scenes=[
            {
                "name": "Movie night",
                "entities": {"light.lamp": {"state": "on", "brightness": 20}},
            },
            {
                "name": "Paused",
                "entities": {"light.lamp": {"state": "on", "brightness": 100}},
            },
        ],
    )
    lounge = world.tv_a.room
    world.bulb = _ent(hass, "light", "bulb", lounge, brightness=200)
    world.dimlamp = _ent(hass, "light", "dimlamp", lounge, brightness=20)
    world.spare = _ent(hass, "light", "spare", lounge, state="off")
    fake.set(world.light_a, True, 255, color_mode="hs", hs_color=HS)
    fake.set(world.bulb, True, 200)
    fake.set(world.dimlamp, True, 20)
    fake.set(world.spare, False)
    world.lamp = world.light_a
    await hass.async_block_till_done()
    return fake


def set_scenes(hass, world, movie=None, paused=None):
    hass.config_entries.async_update_entry(
        world.entry,
        options={
            **world.entry.options,
            "scenes": ["scene.movie_night", "scene.paused"],
            "movie_night_scene": movie,
            "paused_scene": paused,
        },
    )


async def add_lounge_tv(hass, world, tv_id="tv_d"):
    tv = Tv(tv_id, "Lounge TV 2", world.tv_a.room)
    sub = tv.sub()
    hass.config_entries.async_add_subentry(
        world.entry,
        ConfigSubentry(
            data=MappingProxyType(sub["data"]),
            subentry_type=sub["subentry_type"],
            title=sub["title"],
            unique_id=sub["unique_id"],
        ),
    )
    await hass.async_block_till_done()
    return tv


def untouched(lights, world):
    assert lights.level(world.lamp) == ("on", 255)
    assert lights.level(world.bulb) == ("on", 200)
    assert lights.level(world.dimlamp) == ("on", 20)
    assert lights.level(world.spare)[0] == "off"


# --- play, pause, stop -----------------------------------------------------------


async def test_film_dims_brighter_lights_only(client, world, lights):  # noqa: F811
    ws = await connect(client, world.tv_a)
    assert await play(ws, "playing") == APPLIED
    assert lights.level(world.lamp) == ("on", PCT10)
    assert lights.level(world.bulb) == ("on", PCT10)
    assert lights.level(world.dimlamp) == ("on", 20)  # 8 %: already below 10 %
    assert lights.level(world.spare)[0] == "off"  # never switched on


async def test_episode_dims_to_35(client, world, lights):  # noqa: F811
    ws = await connect(client, world.tv_a)
    assert await play(ws, "playing", "episode") == APPLIED
    assert lights.level(world.lamp) == ("on", PCT35)
    assert lights.level(world.bulb) == ("on", PCT35)
    assert lights.level(world.dimlamp) == ("on", 20)
    assert lights.level(world.spare)[0] == "off"


@pytest.mark.parametrize("media", ["music", "other"])
async def test_music_and_other_leave_the_lights(client, world, lights, media):  # noqa: F811
    ws = await connect(client, world.tv_a)
    assert await play(ws, "playing", media) == APPLIED
    untouched(lights, world)
    assert lights.calls == []
    assert await play(ws, "stopped", media, cid=10) == APPLIED
    untouched(lights, world)


async def test_pause_raises_dimmed_lights_to_50(client, world, lights):  # noqa: F811
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    assert await play(ws, "paused", cid=10) == APPLIED
    assert lights.level(world.lamp) == ("on", PCT50)
    assert lights.level(world.bulb) == ("on", PCT50)
    # Never dimmed by Perch, so not lifted; never lit, so still off.
    assert lights.level(world.dimlamp) == ("on", 20)
    assert lights.level(world.spare)[0] == "off"


async def test_pause_does_not_lift_a_light_that_was_dimmer_than_50(  # noqa: F811
    hass, client, world, lights
):
    lights.set(world.bulb, True, 100)  # 39 %, below the paused level
    await hass.async_block_till_done()
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    await play(ws, "paused", cid=10)
    assert lights.level(world.bulb) == ("on", PCT10)


async def test_play_after_pause_dims_again(client, world, lights):  # noqa: F811
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    await play(ws, "paused", cid=10)
    await play(ws, "playing", cid=11)
    assert lights.level(world.lamp) == ("on", PCT10)
    assert lights.level(world.bulb) == ("on", PCT10)


@pytest.mark.parametrize("path", ["stop", "pause_stop", "episode_stop"])
async def test_stop_restores_exact_state(client, world, lights, path):  # noqa: F811
    ws = await connect(client, world.tv_a)
    media = "episode" if path == "episode_stop" else "film"
    await play(ws, "playing", media)
    if path == "pause_stop":
        await play(ws, "paused", media, cid=10)
    assert lights.level(world.lamp) != ("on", 255)
    assert await play(ws, "stopped", media, cid=12) == APPLIED
    untouched(lights, world)
    # The colour attribute came back with the brightness.
    assert list(lights.hass.states.get(world.lamp).attributes["hs_color"]) == HS


async def test_off_light_stays_off_across_a_film(client, world, lights):  # noqa: F811
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    assert all(
        entity != world.spare or service != "turn_on"
        for service, entity, _ in lights.calls
    )
    await play(ws, "stopped", cid=10)
    assert lights.level(world.spare)[0] == "off"


async def test_movie_night_scene_is_used_for_film(  # noqa: F811
    hass, client, world, lights
):
    set_scenes(hass, world, movie="scene.movie_night")
    await hass.async_block_till_done()
    ws = await connect(client, world.tv_a)
    assert await play(ws, "playing") == APPLIED
    assert lights.level(world.lamp) == ("on", 20)  # the scene's value, not 10 %
    assert lights.level(world.bulb) == ("on", 200)  # not in the scene: left
    await play(ws, "stopped", cid=10)
    untouched(lights, world)


async def test_episode_ignores_the_movie_scene(hass, client, world, lights):  # noqa: F811
    set_scenes(hass, world, movie="scene.movie_night")
    await hass.async_block_till_done()
    ws = await connect(client, world.tv_a)
    await play(ws, "playing", "episode")
    assert lights.level(world.lamp) == ("on", PCT35)


async def test_paused_scene_is_used_for_pause(hass, client, world, lights):  # noqa: F811
    set_scenes(hass, world, paused="scene.paused")
    await hass.async_block_till_done()
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    assert lights.level(world.lamp) == ("on", PCT10)
    await play(ws, "paused", cid=10)
    assert lights.level(world.lamp) == ("on", 100)  # the scene, not 50 %
    await play(ws, "stopped", cid=11)
    untouched(lights, world)


# --- restart ---------------------------------------------------------------------


async def test_restart_mid_film_still_restores(hass, client, world, lights):  # noqa: F811
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    assert await hass.config_entries.async_unload(world.entry.entry_id)
    assert await hass.config_entries.async_setup(world.entry.entry_id)
    await hass.async_block_till_done()
    assert lights.level(world.lamp) == ("on", PCT10)  # restart changes nothing
    ws = await connect(client, world.tv_a)
    assert await play(ws, "stopped", cid=10) == APPLIED
    untouched(lights, world)
    assert list(hass.states.get(world.lamp).attributes["hs_color"]) == HS


async def test_restart_restores_from_storage_in_a_fresh_setup(  # noqa: F811
    hass, hass_storage, client, world, lights
):
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    assert await hass.config_entries.async_unload(world.entry.entry_id)
    stored = hass_storage["perch.moods"]["data"]["rooms"][world.tv_a.room]
    assert stored["tvs"] == {"tv_a": "playing"}
    assert stored["snapshot"][world.lamp]["brightness"] == 255
    # The engine's own memory is gone: everything below comes from storage.
    assert await hass.config_entries.async_setup(world.entry.entry_id)
    await hass.async_block_till_done()
    ws = await connect(client, world.tv_a)
    await play(ws, "stopped", cid=10)
    untouched(lights, world)


async def test_hand_change_survives_a_restart(hass, client, world, lights, freezer):  # noqa: F811
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    freezer.tick(30)  # past the hand-change grace window
    lights.set(world.bulb, True, 77)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(world.entry.entry_id)
    assert await hass.config_entries.async_setup(world.entry.entry_id)
    await hass.async_block_till_done()
    ws = await connect(client, world.tv_a)
    await play(ws, "stopped", cid=10)
    assert lights.level(world.bulb) == ("on", 77)
    assert lights.level(world.lamp) == ("on", 255)


async def test_timeout_restore_after_restart_if_no_tv_resends(  # noqa: F811
    hass, client, world, lights
):
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    assert await hass.config_entries.async_unload(world.entry.entry_id)
    assert await hass.config_entries.async_setup(world.entry.entry_id)
    await hass.async_block_till_done()
    assert lights.level(world.lamp) == ("on", PCT10)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=16))
    await hass.async_block_till_done()
    untouched(lights, world)


# --- hand-changed lights -----------------------------------------------------------


async def test_hand_changed_light_keeps_its_value(hass, client, world, lights, freezer):  # noqa: F811
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    freezer.tick(30)  # past the hand-change grace window
    lights.set(world.bulb, True, 200)  # a person turns it back up
    lights.set(world.bulb, True, 150)
    await hass.async_block_till_done()
    await play(ws, "stopped", cid=10)
    assert lights.level(world.bulb) == ("on", 150)
    assert lights.level(world.lamp) == ("on", 255)  # the others restore


async def test_hand_switched_off_light_stays_off(hass, client, world, lights, freezer):  # noqa: F811
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    freezer.tick(30)  # past the hand-change grace window
    lights.set(world.bulb, False)
    await hass.async_block_till_done()
    await play(ws, "stopped", cid=10)
    assert lights.level(world.bulb)[0] == "off"
    assert lights.level(world.lamp) == ("on", 255)


async def test_hand_changed_light_is_not_dimmed_again(hass, client, world, lights, freezer):  # noqa: F811
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    freezer.tick(30)  # past the hand-change grace window
    lights.set(world.bulb, True, 150)
    await hass.async_block_till_done()
    await play(ws, "paused", cid=10)
    await play(ws, "playing", cid=11)
    assert lights.level(world.bulb) == ("on", 150)


async def test_same_value_change_is_not_a_hand_change(hass, client, world, lights):  # noqa: F811
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    lights.set(world.bulb, True, PCT10)  # the value Perch left, from elsewhere
    await hass.async_block_till_done()
    await play(ws, "stopped", cid=10)
    assert lights.level(world.bulb) == ("on", 200)


async def test_unavailable_flap_is_not_a_hand_change(hass, client, world, lights):  # noqa: F811
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    hass.states.async_set(world.bulb, "unavailable")
    await hass.async_block_till_done()
    lights.set(world.bulb, True, PCT10)
    await hass.async_block_till_done()
    await play(ws, "stopped", cid=10)
    assert lights.level(world.bulb) == ("on", 200)


# --- two TVs in one room -----------------------------------------------------------


async def test_two_tvs_restore_only_when_both_stop(client, world, hass, lights):  # noqa: F811
    tv_b = await add_lounge_tv(hass, world)
    ws_a = await connect(client, world.tv_a)
    ws_b = await connect(client, tv_b)
    await play(ws_a, "playing")
    await play(ws_b, "playing")
    assert lights.level(world.lamp) == ("on", PCT10)
    await play(ws_a, "stopped", cid=10)
    assert lights.level(world.lamp) == ("on", PCT10)  # B still watching
    assert lights.level(world.bulb) == ("on", PCT10)
    await play(ws_b, "stopped", cid=10)
    untouched(lights, world)  # and not the dimmed values B's play might have kept


async def test_second_play_does_not_retake_the_snapshot(  # noqa: F811
    client, world, hass, lights
):
    tv_b = await add_lounge_tv(hass, world)
    ws_a = await connect(client, world.tv_a)
    ws_b = await connect(client, tv_b)
    await play(ws_a, "playing")
    await play(ws_b, "playing")
    await play(ws_b, "stopped", cid=10)
    await play(ws_a, "stopped", cid=10)
    assert lights.level(world.lamp) == ("on", 255)  # not the dimmed 26


async def test_pause_by_one_does_not_lift_lights_while_other_plays(  # noqa: F811
    client, world, hass, lights
):
    tv_b = await add_lounge_tv(hass, world)
    ws_a = await connect(client, world.tv_a)
    ws_b = await connect(client, tv_b)
    await play(ws_a, "playing")
    await play(ws_b, "playing")
    await play(ws_b, "paused", cid=10)
    assert lights.level(world.lamp) == ("on", PCT10)
    await play(ws_a, "paused", cid=10)  # nobody playing now
    assert lights.level(world.lamp) == ("on", PCT50)


async def test_second_tv_stop_after_first_paused_restores(  # noqa: F811
    client, world, hass, lights
):
    tv_b = await add_lounge_tv(hass, world)
    ws_a = await connect(client, world.tv_a)
    ws_b = await connect(client, tv_b)
    await play(ws_a, "playing")
    await play(ws_b, "paused")
    await play(ws_a, "stopped", cid=10)
    assert lights.level(world.lamp)[1] != 255  # B still holds a lease
    await play(ws_b, "stopped", cid=10)
    untouched(lights, world)


async def test_tvs_in_other_rooms_do_not_hold_the_lounge(  # noqa: F811
    client, world, lights
):
    ws_a = await connect(client, world.tv_a)
    ws_b = await connect(client, world.tv_b)  # Kitchen
    await play(ws_a, "playing")
    await play(ws_b, "playing")
    await play(ws_a, "stopped", cid=10)
    untouched(lights, world)


# --- gates and reasons -----------------------------------------------------------


async def test_no_room(client, world, lights):  # noqa: F811
    ws = await connect(client, world.tv_c)
    assert await play(ws, "playing") == {"applied": False, "reason": "no_room"}
    untouched(lights, world)


async def test_lighting_off_refuses_and_restores(client, world, lights):  # noqa: F811
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    result = await play(ws, "playing", lighting=False, cid=10)
    assert result == {"applied": False, "reason": "lighting_off"}
    untouched(lights, world)


async def test_lighting_off_leaves_the_room_dark_for_the_other_tv(  # noqa: F811
    client, world, hass, lights
):
    tv_b = await add_lounge_tv(hass, world)
    ws_a = await connect(client, world.tv_a)
    ws_b = await connect(client, tv_b)
    await play(ws_a, "playing")
    await play(ws_b, "playing")
    result = await play(ws_a, "paused", lighting=False, cid=10)
    assert result["reason"] == "lighting_off"
    assert lights.level(world.lamp) == ("on", PCT10)
    await play(ws_b, "stopped", cid=10)
    untouched(lights, world)


async def test_lighting_off_never_dims(client, world, lights):  # noqa: F811
    ws = await connect(client, world.tv_a)
    result = await play(ws, "playing", lighting=False)
    assert result == {"applied": False, "reason": "lighting_off"}
    untouched(lights, world)


async def test_skipped_tonight_reason_and_no_dim(client, world, lights):  # noqa: F811
    ws = await connect(client, world.tv_a)
    until = (dt_util.utcnow() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert (await cmd(ws, 1, "perch/skip_tonight", until=until))["success"]
    assert await play(ws, "playing", cid=2) == {
        "applied": False,
        "reason": "skipped_tonight",
    }
    untouched(lights, world)


async def test_skip_expires(client, world, lights, freezer):  # noqa: F811
    ws = await connect(client, world.tv_a)
    until = (dt_util.utcnow() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await cmd(ws, 1, "perch/skip_tonight", until=until)
    freezer.tick(timedelta(hours=3))
    assert await play(ws, "playing", cid=2) == APPLIED
    assert lights.level(world.lamp) == ("on", PCT10)


async def test_daytime_is_not_after_sunset(hass, client, world, lights):  # noqa: F811
    hass.states.async_set("sun.sun", "above_horizon")
    ws = await connect(client, world.tv_a)
    assert await play(ws, "playing") == {
        "applied": False,
        "reason": "not_after_sunset",
    }
    untouched(lights, world)


async def test_after_sunset_applies(hass, client, world, lights):  # noqa: F811
    hass.states.async_set("sun.sun", "below_horizon")
    ws = await connect(client, world.tv_a)
    assert await play(ws, "playing") == APPLIED
    assert lights.level(world.lamp) == ("on", PCT10)


async def test_missing_sun_counts_as_after_sunset(hass, client, world, lights):  # noqa: F811
    assert hass.states.get("sun.sun") is None
    ws = await connect(client, world.tv_a)
    assert await play(ws, "playing") == APPLIED
    assert lights.level(world.lamp) == ("on", PCT10)


async def test_refused_play_releases_the_lease(hass, client, world, lights):  # noqa: F811
    """Whatever refuses a later play, the room is restored if it was the last."""
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    until = (dt_util.utcnow() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await cmd(ws, 1, "perch/skip_tonight", until=until)
    untouched(lights, world)
    assert await play(ws, "playing", cid=3) == {
        "applied": False,
        "reason": "skipped_tonight",
    }


# --- disconnects -------------------------------------------------------------------


async def test_disconnect_while_playing_restores_after_the_timeout(  # noqa: F811
    hass, client, world, lights
):
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    await ws.close()
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5))
    await hass.async_block_till_done()
    assert lights.level(world.lamp) == ("on", PCT10)  # not yet
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=16))
    await hass.async_block_till_done()
    untouched(lights, world)


async def test_reconnect_before_the_timeout_cancels_the_restore(  # noqa: F811
    hass, client, world, lights
):
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    await ws.close()
    await hass.async_block_till_done()
    ws = await connect(client, world.tv_a)
    await play(ws, "playing", cid=10)  # the TV resends its state
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=16))
    await hass.async_block_till_done()
    assert lights.level(world.lamp) == ("on", PCT10)
    await play(ws, "stopped", cid=11)
    untouched(lights, world)


async def test_disconnect_with_the_other_tv_still_watching(  # noqa: F811
    hass, client, world, lights
):
    tv_b = await add_lounge_tv(hass, world)
    ws_a = await connect(client, world.tv_a)
    ws_b = await connect(client, tv_b)
    await play(ws_a, "playing")
    await play(ws_b, "playing")
    await ws_a.close()
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=16))
    await hass.async_block_till_done()
    assert lights.level(world.lamp) == ("on", PCT10)


# --- skip_tonight ------------------------------------------------------------------


@pytest.mark.parametrize(
    "until",
    [
        lambda n: (n - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        lambda n: (n + timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        lambda n: "tomorrow evening",
        lambda n: (n + timedelta(hours=1)).isoformat(),
        lambda n: None,
    ],
    ids=["past", "too_far", "words", "no_z", "missing"],
)
async def test_skip_tonight_rejects_bad_until(client, world, lights, until):  # noqa: F811
    ws = await connect(client, world.tv_a)
    value = until(dt_util.utcnow())
    fields = {} if value is None else {"until": value}
    data = await cmd(ws, 1, "perch/skip_tonight", **fields)
    assert not data["success"] and data["error"]["code"] == "invalid_format"
    assert await play(ws, "playing", cid=2) == APPLIED  # nothing was skipped


async def test_skip_tonight_releases_the_tvs_lease(client, world, lights):  # noqa: F811
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    until = (dt_util.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = await cmd(ws, 1, "perch/skip_tonight", until=until)
    assert data["success"]
    untouched(lights, world)


async def test_skip_tonight_leaves_other_tvs_alone(  # noqa: F811
    client, world, hass, lights
):
    tv_b = await add_lounge_tv(hass, world)
    ws_a = await connect(client, world.tv_a)
    ws_b = await connect(client, tv_b)
    await play(ws_a, "playing")
    await play(ws_b, "playing")
    until = (dt_util.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await cmd(ws_a, 1, "perch/skip_tonight", until=until)
    assert lights.level(world.lamp) == ("on", PCT10)  # B still holds the room
    assert await play(ws_b, "paused", cid=11) == APPLIED  # B is not skipped
    await play(ws_b, "stopped", cid=12)
    untouched(lights, world)


# --- removal and room change -------------------------------------------------------


async def test_removing_a_tv_releases_its_lease(hass, client, world, lights):  # noqa: F811
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    sub = next(
        s for s in world.entry.subentries.values() if s.data["tv_id"] == "tv_a"
    )
    hass.config_entries.async_remove_subentry(world.entry, sub.subentry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    untouched(lights, world)


async def test_removing_one_of_two_tvs_keeps_the_room_dark(  # noqa: F811
    hass, client, world, lights
):
    tv_b = await add_lounge_tv(hass, world)
    ws_a = await connect(client, world.tv_a)
    ws_b = await connect(client, tv_b)
    await play(ws_a, "playing")
    await play(ws_b, "playing")
    sub = next(s for s in world.entry.subentries.values() if s.data["tv_id"] == "tv_a")
    hass.config_entries.async_remove_subentry(world.entry, sub.subentry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert lights.level(world.lamp) == ("on", PCT10)
    await play(ws_b, "stopped", cid=10)
    untouched(lights, world)


async def test_moving_a_tv_to_another_room_releases_the_old_room(  # noqa: F811
    hass, client, world, lights
):
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    sub = next(s for s in world.entry.subentries.values() if s.data["tv_id"] == "tv_a")
    kitchen = world.tv_b.room
    hass.config_entries.async_update_subentry(
        world.entry, sub, data={**sub.data, "room_id": kitchen, "rooms": [kitchen]}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    untouched(lights, world)


# --- review fixes: reconnect, hand-change tolerance, unload, skip on room clear -------


async def test_reconnect_without_playback_still_restores(  # noqa: F811
    hass, client, world, lights
):
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    await ws.close()
    await hass.async_block_till_done()
    ws = await connect(client, world.tv_a)  # reconnects and sends nothing
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=16))
    await hass.async_block_till_done()
    untouched(lights, world)


async def test_off_by_two_brightness_is_not_a_hand_change(  # noqa: F811
    hass, client, world, lights, freezer
):
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    freezer.tick(30)
    lights.set(world.bulb, True, PCT10 + 2)
    await hass.async_block_till_done()
    await play(ws, "stopped", cid=10)
    assert lights.level(world.bulb) == ("on", 200)


async def test_change_inside_the_grace_window_is_not_a_hand_change(  # noqa: F811
    hass, client, world, lights, freezer
):
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    freezer.tick(5)
    lights.set(world.bulb, True, 150)  # another context, 5 s after a Perch call
    await hass.async_block_till_done()
    await play(ws, "stopped", cid=10)
    assert lights.level(world.bulb) == ("on", 200)


async def test_change_after_the_grace_window_is_a_hand_change(  # noqa: F811
    hass, client, world, lights, freezer
):
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    freezer.tick(30)
    lights.set(world.bulb, True, 150)
    await hass.async_block_till_done()
    await play(ws, "stopped", cid=10)
    assert lights.level(world.bulb) == ("on", 150)


async def test_unload_waits_for_an_inflight_playback(  # noqa: F811
    hass, client, world, lights
):
    moods = get_runtime(hass).moods
    release = asyncio.Event()
    entered = asyncio.Event()
    saves_after_unload: list[bool] = []
    real_call = moods._call

    async def slow_call(*args, **kwargs):
        entered.set()
        await release.wait()
        await real_call(*args, **kwargs)

    async def counting_save(data):
        saves_after_unload.append(moods._unloaded)

    moods._call = slow_call
    moods._store.async_save = counting_save
    ws = await connect(client, world.tv_a)
    playing = asyncio.create_task(play(ws, "playing"))
    await entered.wait()
    unloading = asyncio.create_task(moods.async_unload())
    await asyncio.sleep(0)
    assert not unloading.done()  # waits for the playback to finish
    release.set()
    await playing
    await unloading
    # Every save before the final flush happened before unload; none after it.
    assert saves_after_unload[-1] is True
    assert saves_after_unload.count(True) == 1
    scope = build_scope(hass, world.entry, "tv_a")
    assert await moods.async_playback(scope, "playing", "film", True) == {
        "applied": False,
        "reason": "unloaded",
    }


async def test_clearing_a_tvs_room_keeps_its_skip_tonight(  # noqa: F811
    hass, client, world, lights
):
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    until = (dt_util.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await cmd(ws, 1, "perch/skip_tonight", until=until)
    await play(ws, "playing", cid=2)  # skipped: no lease, skip stays
    sub = next(s for s in world.entry.subentries.values() if s.data["tv_id"] == "tv_a")
    hass.config_entries.async_update_subentry(
        world.entry, sub, data={**sub.data, "room_id": None, "rooms": []}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert "tv_a" in get_runtime(hass).moods.known_tvs()


async def test_clearing_a_tvs_room_releases_its_leases(  # noqa: F811
    hass, client, world, lights
):
    ws = await connect(client, world.tv_a)
    await play(ws, "playing")
    sub = next(s for s in world.entry.subentries.values() if s.data["tv_id"] == "tv_a")
    hass.config_entries.async_update_subentry(
        world.entry, sub, data={**sub.data, "room_id": None, "rooms": []}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    untouched(lights, world)
