"""`perch/subscribe`, the doorbell push and the states and manifest pushes."""

from __future__ import annotations

import asyncio

from homeassistant.helpers import entity_registry as er

from .fakes import setup_lights
from .schema import validate
from .test_endpoint import _ent, client, cmd, connect, recv, world  # noqa: F401

BELL = "binary_sensor.front_bell"


def _options(world, **extra):
    return {
        "cameras": [world.camera],
        "doorbell": BELL,
        "doorbell_camera": world.camera,
        "scenes": [],
        **extra,
    }


async def _arm(hass, world, state="off", **extra):
    hass.states.async_set(BELL, state)
    hass.config_entries.async_update_entry(world.entry, options=_options(world, **extra))
    await hass.async_block_till_done()


async def _no_frame(ws, wait=0.3):
    try:
        msg = await ws.receive(timeout=wait)
    except TimeoutError:
        return
    raise AssertionError(f"unexpected frame {msg}")


async def test_press_reaches_every_subscribed_tv(hass, client, world):
    await _arm(hass, world)
    a1 = await connect(client, world.tv_a)
    a2 = await connect(client, world.tv_a)
    b = await connect(client, world.tv_b)
    quiet = await connect(client, world.tv_a)
    for ws, cid in ((a1, 3), (a2, 4), (b, 5)):
        data = await cmd(ws, cid, "perch/subscribe", topics=["doorbell"])
        assert data["result"] == {"topics": ["doorbell"]}
    await cmd(quiet, 6, "perch/subscribe", topics=["manifest"])
    start = hass.loop.time()
    hass.states.async_set(BELL, "on")
    frames = [await recv(ws, "EventFrame") for ws in (a1, a2, b)]
    assert hass.loop.time() - start < 2
    for frame, cid in zip(frames, (3, 4, 5)):
        assert frame["id"] == cid
        assert frame["event"]["topic"] == "doorbell"
        assert frame["event"]["data"]["camera_id"] == world.camera
    validate("DoorbellEventFrame", frames[0])
    await _no_frame(quiet)
    from custom_components.perch.runtime import get_runtime

    assert get_runtime(hass).hub.last_ring is not None


async def test_camera_id_null_when_unset(hass, client, world):
    await _arm(hass, world, doorbell_camera=None)
    ws = await connect(client, world.tv_a)
    await cmd(ws, 3, "perch/subscribe", topics=["doorbell"])
    hass.states.async_set(BELL, "on")
    frame = await recv(ws, "EventFrame")
    assert frame["event"]["data"]["camera_id"] is None


async def test_event_entity_press(hass, client, world):
    hass.states.async_set("event.bell", "2026-11-15T20:00:00.000+00:00")
    hass.config_entries.async_update_entry(
        world.entry, options=_options(world, doorbell="event.bell")
    )
    await hass.async_block_till_done()
    ws = await connect(client, world.tv_a)
    await cmd(ws, 3, "perch/subscribe", topics=["doorbell"])
    hass.states.async_set("event.bell", "2026-11-15T20:05:00.000+00:00")
    frame = await recv(ws, "EventFrame")
    assert frame["event"]["topic"] == "doorbell"


async def test_unavailable_flaps_ignored(hass, client, world):
    await _arm(hass, world)
    ws = await connect(client, world.tv_a)
    await cmd(ws, 3, "perch/subscribe", topics=["doorbell"])
    for state in ("unavailable", "on", "unknown", "on"):
        hass.states.async_set(BELL, state)
    await hass.async_block_till_done()
    await _no_frame(ws)
    # on -> off -> on is a real press.
    hass.states.async_set(BELL, "off")
    hass.states.async_set(BELL, "on")
    assert (await recv(ws, "EventFrame"))["event"]["topic"] == "doorbell"


async def test_resubscribe_moves_id_and_adds_topics(hass, client, world):
    await _arm(hass, world)
    ws = await connect(client, world.tv_a)
    await cmd(ws, 3, "perch/subscribe", topics=["doorbell"])
    data = await cmd(ws, 8, "perch/subscribe", topics=["states", "doorbell"])
    assert data["result"]["topics"] == ["doorbell", "states"]
    hass.states.async_set(BELL, "on")
    assert (await recv(ws, "EventFrame"))["id"] == 8


async def test_invalid_topics(client, world):
    ws = await connect(client, world.tv_a)
    for topics in ([], ["nope"], ["states", "states"]):
        data = await cmd(ws, 3, "perch/subscribe", topics=topics)
        assert data["error"]["code"] == "invalid_format"


async def test_states_push_only_for_scope_lights(hass, client, world):
    ws = await connect(client, world.tv_a)
    await cmd(ws, 3, "perch/subscribe", topics=["states"])
    hass.states.async_set(world.light_b, "on")  # the kitchen TV's light
    hass.states.async_set(world.light_a, "on", {"brightness": 102})
    frame = await recv(ws, "EventFrame")
    validate("StatesEventFrame", frame)
    assert frame["event"]["data"]["lights"] == [
        {"id": world.light_a, "on": True, "brightness": 40, "available": True}
    ]
    await _no_frame(ws)


async def test_manifest_push_after_options_update(hass, client, world):
    ws = await connect(client, world.tv_a)
    await cmd(ws, 3, "perch/subscribe", topics=["manifest"])
    hass.config_entries.async_update_entry(world.entry, options=_options(world))
    frame = await recv(ws, "EventFrame")
    validate("ManifestEventFrame", frame)
    assert frame["event"]["data"] == {}


async def test_events_topic_accepted_nothing_pushed(hass, client, world):
    await _arm(hass, world)
    ws = await connect(client, world.tv_a)
    data = await cmd(ws, 3, "perch/subscribe", topics=["events"])
    assert data["result"] == {"topics": ["events"]}
    hass.states.async_set(BELL, "on")
    hass.states.async_set(world.light_a, "on")
    await _no_frame(ws)


async def test_subscriptions_dropped_on_close(hass, client, world):
    from custom_components.perch.runtime import get_runtime

    ws = await connect(client, world.tv_a)
    await cmd(ws, 3, "perch/subscribe", topics=["states"])
    hub = get_runtime(hass).hub
    assert len(hub._subs) == 1
    await ws.close()
    for _ in range(50):
        if not hub._subs:
            break
        await asyncio.sleep(0.05)
    assert not hub._subs
