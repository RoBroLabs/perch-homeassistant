"""`perch/events` and the companion's own media URLs, against a fake Frigate.

No real Frigate integration is loaded: a config entry for the domain, a fake
client in `hass.data["frigate"]`, and upstream media from `aioclient_mock`.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .schema import validate
from .test_endpoint import client, cmd, connect, world  # noqa: F401  (fixtures)

AUTH_VALUE = "Bearer FRIGATE-SECRET-VALUE"
FRIGATE_URL = "http://frigate.example.invalid:5000"
START = 1_700_000_000.5  # 2023-11-14T22:13:20Z
DAY = "2023-11-14"
JPEG = b"\xff\xd8\xff-fake-jpeg"
MP4 = b"0123456789" * 10


class FakeFrigateClient:
    validate_ssl = False

    def __init__(self, events=None, error=None):
        self.events = events if events is not None else []
        self.error = error
        self.calls: list[dict] = []

    async def async_get_events(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return list(self.events)

    async def get_auth_headers(self):
        return {"Authorization": AUTH_VALUE}


def _event(eid, camera, start=START, end=START + 30, snap=True, clip=True):
    return {
        "id": eid,
        "camera": camera,
        "label": "person",
        "start_time": start,
        "end_time": end,
        "has_snapshot": snap,
        "has_clip": clip,
    }


@pytest.fixture
async def frigate(hass, world):  # noqa: F811
    f = type("F", (), {})()
    f.entry = MockConfigEntry(
        domain="frigate", data={"url": FRIGATE_URL}, entry_id="frig1"
    )
    f.entry.add_to_hass(hass)
    reg = er.async_get(hass)
    ids = {}
    for name in ("front", "back", "side"):
        e = reg.async_get_or_create(
            "camera", "frigate", f"frig1:camera:{name}", suggested_object_id=f"fr_{name}"
        )
        hass.states.async_set(e.entity_id, "idle", {"friendly_name": name})
        ids[name] = e.entity_id
    f.front, f.back, f.side = ids["front"], ids["back"], ids["side"]
    # `side` is a Frigate camera that is not curated.
    hass.config_entries.async_update_entry(
        world.entry,
        options={**world.entry.options, "cameras": [f.front, f.back, world.camera]},
    )
    await hass.async_block_till_done()
    f.client = FakeFrigateClient(
        [
            _event("ev1", "front", START),
            _event("ev2", "back", START + 100, end=None, clip=False),
            _event("ev3", "front", START + 50, snap=False),
            _event("ev4", "side", START + 60),
        ]
    )
    hass.data["frigate"] = {"frig1": {"client": f.client}, "other": "not an entry"}
    with patch(
        "custom_components.perch.frigate.async_get_integration",
        return_value=type("I", (), {"version": "5.15.6"})(),
    ):
        yield f


def _scan(*frames):
    text = json.dumps(frames)
    for bad in ("/api/frigate", "frigate.example.invalid", "notifications", AUTH_VALUE, "5000"):
        assert bad not in text


async def _events(client, world, **fields):  # noqa: F811
    ws = await connect(client, world.tv_a)
    fields = {"day": DAY, "role": "adult"} | fields
    data = await cmd(ws, 5, "perch/events", **fields)
    await ws.close()
    return data


async def test_events_return_and_validate(client, world, frigate):  # noqa: F811
    data = await _events(client, world)
    assert data["success"]
    events = data["result"]["events"]
    validate("EventsResultFrame", data)
    # Newest first; ev3 has no snapshot (the schema needs one); ev4 is not curated.
    assert [e["id"] for e in events] == ["ev2", "ev1"]
    assert events[0]["camera_id"] == frigate.back
    assert events[0]["ended_at"] is None and events[0]["clip_url"] is None
    assert events[1]["started_at"] == "2023-11-14T22:13:20Z"
    assert events[1]["clip_url"].startswith("/api/perch/media/")
    assert frigate.client.calls[0]["cameras"] == ["back", "front"]
    assert frigate.client.calls[0]["limit"] == 100
    _scan(data)


async def test_camera_filter(client, world, frigate):  # noqa: F811
    data = await _events(client, world, camera_id=frigate.front)
    assert [e["id"] for e in data["result"]["events"]] == ["ev1"]
    assert frigate.client.calls[0]["cameras"] == ["front"]


async def test_teen_allowed(client, world, frigate):  # noqa: F811
    assert (await _events(client, world, role="teen"))["success"]


async def test_kid_refused_without_calling_frigate(client, world, frigate):  # noqa: F811
    data = await _events(client, world, role="kid")
    assert not data["success"] and data["error"]["code"] == "refused_for_role"
    assert frigate.client.calls == []


async def test_other_camera_not_allowed(client, world, frigate):  # noqa: F811
    for camera in (frigate.side, world.camera_other, "camera.nope"):
        data = await _events(client, world, camera_id=camera)
        assert data["error"]["code"] == "not_allowed"
    assert frigate.client.calls == []


async def test_missing_frigate_unavailable(client, world, frigate, hass):  # noqa: F811
    hass.data.pop("frigate")
    data = await _events(client, world)
    assert data["error"]["code"] == "unavailable"


async def test_unsupported_version_unavailable(client, world, frigate):  # noqa: F811
    with patch(
        "custom_components.perch.frigate.async_get_integration",
        return_value=type("I", (), {"version": "6.0.0"})(),
    ):
        data = await _events(client, world)
    assert data["error"]["code"] == "unavailable"
    assert frigate.client.calls == []


async def test_client_error_unavailable(client, world, frigate):  # noqa: F811
    frigate.client.error = RuntimeError("boom with http://frigate.example.invalid:5000")
    data = await _events(client, world)
    assert data["error"]["code"] == "unavailable"
    _scan(data)


async def _urls(client, world):  # noqa: F811
    data = await _events(client, world, camera_id=world_front(world))
    return data["result"]["events"][0]


def world_front(world):
    return world.front_id


async def test_media_serves_then_expires(client, world, frigate, aioclient_mock):  # noqa: F811
    world.front_id = frigate.front
    event = await _urls(client, world)
    aioclient_mock.get(f"{FRIGATE_URL}/api/events/ev1/snapshot.jpg", content=JPEG)
    resp = await client.get(event["snapshot_url"])
    assert resp.status == 200
    assert resp.content_type == "image/jpeg"
    assert await resp.read() == JPEG
    # The fetch used the integration's auth, which never reaches the TV.
    assert aioclient_mock.mock_calls[-1][3]["Authorization"] == AUTH_VALUE
    _scan(dict(resp.headers))
    later = dt_util.utcnow() + timedelta(minutes=5, seconds=1)
    with patch("homeassistant.util.dt.utcnow", return_value=later):
        assert (await client.get(event["snapshot_url"])).status == 404


async def test_media_expiry_matches_expires_at(client, world, frigate):  # noqa: F811
    world.front_id = frigate.front
    event = await _urls(client, world)
    expires = dt_util.parse_datetime(event["expires_at"])
    assert timedelta(minutes=4) < expires - dt_util.utcnow() <= timedelta(minutes=5)


async def test_unknown_token_and_kind_mismatch_404(client, world, frigate):  # noqa: F811
    world.front_id = frigate.front
    event = await _urls(client, world)
    assert (await client.get("/api/perch/media/nope/snapshot")).status == 404
    clip_token = event["clip_url"].split("/")[4]
    assert (await client.get(f"/api/perch/media/{clip_token}/snapshot")).status == 404


async def test_clip_range_gives_206(client, world, frigate, aioclient_mock):  # noqa: F811
    world.front_id = frigate.front
    event = await _urls(client, world)
    aioclient_mock.get(
        f"{FRIGATE_URL}/api/events/ev1/clip.mp4",
        content=MP4[:10],
        status=206,
        headers={
            "Content-Range": "bytes 0-9/100",
            "Content-Length": "10",
            "Accept-Ranges": "bytes",
        },
    )
    resp = await client.get(event["clip_url"], headers={"Range": "bytes=0-9"})
    assert resp.status == 206
    assert resp.headers["Content-Range"] == "bytes 0-9/100"
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert resp.content_type == "video/mp4"
    assert await resp.read() == MP4[:10]
    assert aioclient_mock.mock_calls[-1][3]["Range"] == "bytes=0-9"


class _FakeContent:
    def __init__(self, chunks, fail=None):
        self._chunks = chunks
        self._fail = fail

    async def iter_chunked(self, _size):
        for chunk in self._chunks:
            yield chunk
        if self._fail is not None:
            raise self._fail


class _FakeUpstream:
    def __init__(self, chunks, status=200, headers=None, fail=None):
        self.status = status
        self.headers = headers or {}
        self.content = _FakeContent(chunks, fail)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_session(upstream):
    class _Session:
        def get(self, *args, **kwargs):
            return upstream

    return patch(
        "custom_components.perch.views.async_get_clientsession",
        return_value=_Session(),
    )


async def test_snapshot_in_several_chunks_arrives_whole(client, world, frigate):  # noqa: F811
    world.front_id = frigate.front
    event = await _urls(client, world)
    chunks = [JPEG[:3], JPEG[3:5], JPEG[5:]]
    with _fake_session(_FakeUpstream(chunks)):
        resp = await client.get(event["snapshot_url"])
    assert resp.status == 200
    assert await resp.read() == JPEG


async def test_oversize_snapshot_gives_502(client, world, frigate):  # noqa: F811
    world.front_id = frigate.front
    event = await _urls(client, world)
    chunk = b"x" * 1024
    with (
        patch("custom_components.perch.views.SNAPSHOT_MAX", 2048),
        _fake_session(_FakeUpstream([chunk, chunk, chunk])),
    ):
        resp = await client.get(event["snapshot_url"])
    assert resp.status == 502


async def test_clip_without_upstream_accept_ranges(client, world, frigate):  # noqa: F811
    world.front_id = frigate.front
    event = await _urls(client, world)
    with _fake_session(_FakeUpstream([MP4], headers={"Content-Encoding": "gzip", "Content-Length": "3"})):
        resp = await client.get(event["clip_url"])
    assert resp.status == 200
    assert "Accept-Ranges" not in resp.headers
    assert resp.headers.get("Content-Length") != "3"


async def test_clip_upstream_fails_mid_stream(client, world, frigate):  # noqa: F811
    world.front_id = frigate.front
    event = await _urls(client, world)
    upstream = _FakeUpstream([MP4[:4]], fail=aiohttp.ClientPayloadError("cut"))
    with _fake_session(upstream):
        try:
            resp = await client.get(event["clip_url"])
            await resp.read()
        except aiohttp.ClientError:
            pass  # the truncated stream may surface to the client; nothing else may
    # The server did not crash or send a second response: it still serves.
    assert (await client.get("/api/perch/media/nope/clip")).status == 404


async def test_media_non_local_refused(client, world, frigate):  # noqa: F811
    world.front_id = frigate.front
    event = await _urls(client, world)
    with patch("custom_components.perch.access.is_local", return_value=False):
        assert (await client.get(event["snapshot_url"])).status == 403


async def test_media_camera_removed_from_scope_404(client, world, frigate, hass):  # noqa: F811
    world.front_id = frigate.front
    event = await _urls(client, world)
    hass.config_entries.async_update_entry(
        world.entry, options={**world.entry.options, "cameras": [frigate.back]}
    )
    await hass.async_block_till_done()
    assert (await client.get(event["snapshot_url"])).status == 404
