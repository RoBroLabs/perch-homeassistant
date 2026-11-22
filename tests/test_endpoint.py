"""The TV WebSocket `/api/perch/ws`, its auth proof and the commands.

Every frame the companion sends is validated against the protocol schema.
"""

from __future__ import annotations

import base64
import logging
import os
from datetime import timedelta
from unittest.mock import patch

from aiohttp import WSMsgType, web
from aiohttp.client_exceptions import WSServerHandshakeError
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.perch import async_remove_config_entry_device, crypto
from custom_components.perch.const import DOMAIN

from .fakes import setup_lights
from .schema import validate
from .test_crypto import seal_auth_proof

b64 = base64.b64encode
RESULT_DEFS = {
    "perch/manifest": "ManifestResultFrame",
    "perch/states": "StatesResultFrame",
    "perch/camera": "CameraResultFrame",
    "perch/playback": "PlaybackResultFrame",
    "perch/skip_tonight": "SkipTonightResultFrame",
    "perch/events": "EventsResultFrame",
    "perch/subscribe": "SubscribeResultFrame",
    "perch/light": "LightResultFrame",
    "perch/scene": "SceneResultFrame",
    "perch/notify": "NotifyResultFrame",
    "perch/let_in": "LetInResultFrame",
}


PLAYBACK = {
    "id": 9, "type": "perch/playback", "state": "playing",
    "media": "film", "lighting": True,
}


class Tv:
    def __init__(self, tv_id, name, room):
        self.tv_id, self.name, self.room = tv_id, name, room
        self.priv = X25519PrivateKey.generate()
        self.pub = self.priv.public_key().public_bytes_raw()
        self.credential = crypto.new_credential()
        self.hash = crypto.credential_hash(self.credential)

    def sub(self, no_room=False):
        data = {
            "tv_id": self.tv_id,
            "public_key": b64(self.pub).decode(),
            "credential_hash": self.hash,
        }
        if not no_room:
            data |= {"room_id": self.room, "rooms": [self.room]}
        return ConfigSubentryData(
            data=data,
            subentry_type="tv",
            title=self.name,
            unique_id=self.tv_id,
        )

    def proof(self, auth_required, credential=None, stored_hash=None):
        return seal_auth_proof(
            self.priv,
            base64.b64decode(auth_required["server_key"]),
            stored_hash or self.hash,
            base64.b64decode(auth_required["challenge"]),
            credential or self.credential,
        )

    def auth_frame(self, auth_required, **kw):
        frame = {
            "type": "auth",
            "protocol": 1,
            "tv_id": self.tv_id,
            "proof": b64(self.proof(auth_required, **kw)).decode(),
        }
        validate("AuthFrame", frame)
        return frame


def _ent(hass, domain, key, area=None, state="on", **attrs):
    reg = er.async_get(hass)
    e = reg.async_get_or_create(domain, "test", key, suggested_object_id=key)
    if area:
        reg.async_update_entity(e.entity_id, area_id=area)
    hass.states.async_set(e.entity_id, state, {"friendly_name": key.title(), **attrs})
    return e.entity_id


@pytest.fixture
async def world(hass):
    assert await async_setup_component(hass, "http", {})
    # HA's own endpoints, so the credential can be tried against them.
    assert await async_setup_component(hass, "websocket_api", {})
    assert await async_setup_component(hass, "api", {})
    areas = ar.async_get(hass)
    lounge, kitchen = areas.async_create("Lounge").id, areas.async_create("Kitchen").id
    w = type("W", (), {})()
    w.tv_a, w.tv_b = Tv("tv_a", "Lounge TV", lounge), Tv("tv_b", "Kitchen TV", kitchen)
    w.tv_c = Tv("tv_c", "Hall TV", "")  # no room picked yet
    w.light_a = _ent(hass, "light", "lamp", lounge, brightness=128)
    w.light_b = _ent(hass, "light", "hob", kitchen)
    w.camera = _ent(hass, "camera", "front", None, "idle", access_token="tok123")
    w.camera_other = _ent(hass, "camera", "garage", None, "idle", access_token="tok9")
    w.entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={},
        options={"cameras": [w.camera], "doorbell": "", "scenes": []},
        subentries_data=[w.tv_a.sub(), w.tv_b.sub(), w.tv_c.sub(no_room=True)],
    )
    w.entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(w.entry.entry_id)
    await hass.async_block_till_done()
    return w


@pytest.fixture
async def client(world, hass_client_no_auth):
    """The test client; sockets it opens are closed at teardown so no
    heartbeat timer outlives the test."""
    # aiohttp leaves a heartbeat timer behind after a server-side close, which
    # HA's lingering-timer check rejects; test_heartbeat_is_thirty_seconds
    # covers the real value.
    with patch("custom_components.perch.endpoint.HEARTBEAT", None):
        client = await hass_client_no_auth()
        async for c in _wrapped(client):
            yield c


async def _wrapped(client):
    opened = []
    connect_ws = client.ws_connect

    async def ws_connect(*args, **kwargs):
        ws = await connect_ws(*args, **kwargs)
        opened.append(ws)
        return ws

    client.ws_connect = ws_connect
    yield client
    for ws in opened:
        await ws.close()


async def test_heartbeat_is_thirty_seconds(client, world):
    with patch(
        "custom_components.perch.endpoint.HEARTBEAT", 30
    ), patch(
        "custom_components.perch.endpoint.web.WebSocketResponse",
        wraps=web.WebSocketResponse,
    ) as ws_class:
        ws = await client.ws_connect("/api/perch/ws")
        await recv(ws, "AuthRequiredFrame")
        await ws.close()
    ws_class.assert_called_once_with(heartbeat=30)


async def recv(ws, schema_def=None):
    msg = await ws.receive(timeout=5)
    assert msg.type is WSMsgType.TEXT, msg
    data = msg.json()
    if schema_def:
        validate(schema_def, data)
    return data


async def connect(client, tv, **proof_kw):
    """Connect and authenticate; returns the socket after auth_ok."""
    ws = await client.ws_connect("/api/perch/ws")
    required = await recv(ws, "AuthRequiredFrame")
    await ws.send_json(tv.auth_frame(required, **proof_kw))
    ok = await recv(ws, "AuthOkFrame")
    assert ok["type"] == "auth_ok"
    return ws


async def cmd(ws, cid, ctype, **fields):
    await ws.send_json({"id": cid, "type": ctype, **fields})
    data = await recv(ws)
    assert data["id"] == cid
    validate(
        RESULT_DEFS[ctype] if data["success"] else "ErrorResultFrame", data
    )
    return data


async def expect_auth_invalid(ws):
    assert await recv(ws, "AuthInvalidFrame") == {"type": "auth_invalid"}
    msg = await ws.receive(timeout=5)
    assert msg.type is WSMsgType.CLOSE and msg.data == 4001


# --- auth ---------------------------------------------------------------------


async def test_auth_ok_and_ping(client, world):
    ws = await connect(client, world.tv_a)
    await ws.send_json({"id": 7, "type": "ping"})
    assert await recv(ws, "PongFrame") == {"id": 7, "type": "pong"}
    await ws.close()


async def test_wrong_credential_refused(client, world):
    ws = await client.ws_connect("/api/perch/ws")
    required = await recv(ws, "AuthRequiredFrame")
    await ws.send_json(
        world.tv_a.auth_frame(required, credential=crypto.new_credential())
    )
    await expect_auth_invalid(ws)


async def test_proof_made_with_wrong_credential_hash_refused(client, world):
    ws = await client.ws_connect("/api/perch/ws")
    required = await recv(ws, "AuthRequiredFrame")
    wrong = crypto.credential_hash(crypto.new_credential())
    await ws.send_json(world.tv_a.auth_frame(required, stored_hash=wrong))
    await expect_auth_invalid(ws)


async def test_proof_for_another_tv_id_refused(client, world):
    ws = await client.ws_connect("/api/perch/ws")
    required = await recv(ws, "AuthRequiredFrame")
    frame = world.tv_a.auth_frame(required)
    frame["tv_id"] = "tv_b"
    await ws.send_json(frame)
    await expect_auth_invalid(ws)


async def test_unknown_tv_id_is_auth_invalid(client, world):
    ghost = Tv("tv_ghost", "Ghost", "x")
    ws = await client.ws_connect("/api/perch/ws")
    required = await recv(ws, "AuthRequiredFrame")
    await ws.send_json(ghost.auth_frame(required))
    await expect_auth_invalid(ws)


async def test_unknown_tv_runs_the_same_proof_path(client, world):
    ghost = Tv("tv_ghost", "Ghost", "x")
    with patch(
        "custom_components.perch.endpoint.open_auth_proof", wraps=crypto.open_auth_proof
    ) as spy:
        ws = await client.ws_connect("/api/perch/ws")
        required = await recv(ws, "AuthRequiredFrame")
        await ws.send_json(ghost.auth_frame(required))
        await expect_auth_invalid(ws)
    assert spy.call_count == 1
    assert spy.call_args.args[1] is None and spy.call_args.args[2] is None


async def test_auth_timeout(client, world):
    with patch("custom_components.perch.endpoint.AUTH_TIMEOUT", 0.2):
        ws = await client.ws_connect("/api/perch/ws")
        await recv(ws, "AuthRequiredFrame")
        await expect_auth_invalid(ws)


async def test_frame_before_auth(client, world):
    ws = await client.ws_connect("/api/perch/ws")
    await recv(ws, "AuthRequiredFrame")
    await ws.send_json({"id": 1, "type": "perch/manifest"})
    await expect_auth_invalid(ws)


async def test_wrong_protocol_number_refused(client, world):
    ws = await client.ws_connect("/api/perch/ws")
    required = await recv(ws, "AuthRequiredFrame")
    frame = world.tv_a.auth_frame(required)
    frame["protocol"] = 2
    await ws.send_json(frame)
    await expect_auth_invalid(ws)


async def test_fresh_challenge_and_key_each_connection(client, world):
    seen = []
    for _ in range(2):
        ws = await client.ws_connect("/api/perch/ws")
        seen.append(await recv(ws, "AuthRequiredFrame"))
        await ws.close()
    assert seen[0]["challenge"] != seen[1]["challenge"]
    assert seen[0]["server_key"] != seen[1]["server_key"]


# --- access and throttle ------------------------------------------------------


async def test_remote_address_refused_before_upgrade(client, world):
    with patch("custom_components.perch.access.is_local", return_value=False):
        with pytest.raises(WSServerHandshakeError) as err:
            await client.ws_connect("/api/perch/ws")
    assert err.value.status == 403


async def test_cloud_refused_with_switch_off_allowed_with_it_on(
    hass, client, world
):
    with patch("custom_components.perch.access.is_cloud_connection", return_value=True):
        with pytest.raises(WSServerHandshakeError) as err:
            await client.ws_connect("/api/perch/ws")
        assert err.value.status == 403
        hass.config_entries.async_update_entry(
            world.entry, options={**world.entry.options, "allow_cloud": True}
        )
        await hass.async_block_till_done()
        ws = await connect(client, world.tv_a)
        await ws.close()


async def test_failed_auths_are_throttled_and_never_banned(client, world):
    with patch("homeassistant.components.http.ban.process_wrong_login") as wrong:
        for _ in range(10):
            ws = await client.ws_connect("/api/perch/ws")
            await recv(ws, "AuthRequiredFrame")
            await ws.send_json({"type": "auth", "protocol": 1, "tv_id": "x", "proof": "AAAA"})
            await expect_auth_invalid(ws)
        with pytest.raises(WSServerHandshakeError) as err:
            await client.ws_connect("/api/perch/ws")
    assert err.value.status == 429
    wrong.assert_not_called()


async def test_unloaded_entry_is_not_found(hass, client, world):
    await hass.config_entries.async_unload(world.entry.entry_id)
    with pytest.raises(WSServerHandshakeError) as err:
        await client.ws_connect("/api/perch/ws")
    assert err.value.status == 404


async def test_unload_closes_open_sockets(hass, client, world):
    ws = await connect(client, world.tv_a)
    await hass.config_entries.async_unload(world.entry.entry_id)
    msg = await ws.receive(timeout=5)
    assert msg.type is WSMsgType.CLOSE


_WS_RESPONSE = web.WebSocketResponse


@patch(
    "homeassistant.components.websocket_api.http.web.WebSocketResponse",
    lambda **kw: _WS_RESPONSE(**{**kw, "heartbeat": None}),
)
async def test_credential_useless_on_ha_websocket_and_rest(hass, client, world):
    token = b64(world.tv_a.credential).decode()
    ws = await client.ws_connect("/api/websocket")
    assert (await ws.receive_json())["type"] == "auth_required"
    await ws.send_json({"type": "auth", "access_token": token})
    assert (await ws.receive_json())["type"] == "auth_invalid"
    for bearer in (token, world.tv_a.credential.hex(), world.tv_a.hash):
        resp = await client.get("/api/states", headers={"Authorization": f"Bearer {bearer}"})
        assert resp.status == 401


# --- commands -----------------------------------------------------------------


async def test_manifest(client, world):
    ws = await connect(client, world.tv_a)
    data = await cmd(ws, 1, "perch/manifest")
    result = data["result"]
    assert result["tv_id"] == "tv_a"
    assert [light["id"] for light in result["lights"]] == [world.light_a]
    assert [c["id"] for c in result["cameras"]] == [world.camera]


async def test_states(client, world):
    ws = await connect(client, world.tv_a)
    data = await cmd(ws, 1, "perch/states")
    assert data["result"] == {
        "lights": [{"id": world.light_a, "on": True, "brightness": 50, "available": True}]
    }
    # unknown fields are ignored (protocol README): only the TV's own lights
    data = await cmd(ws, 2, "perch/states", entity_ids=[world.light_b, world.camera])
    assert [x["id"] for x in data["result"]["lights"]] == [world.light_a]


async def test_camera_snapshot_and_unavailable_stream(client, world):
    ws = await connect(client, world.tv_a)
    data = await cmd(ws, 1, "perch/camera", camera_id=world.camera, kind="snapshot")
    assert data["result"]["url"] == f"/api/camera_proxy/{world.camera}?token=tok123"
    data = await cmd(ws, 2, "perch/camera", camera_id=world.camera, kind="stream")
    assert not data["success"] and data["error"]["code"] == "unavailable"


async def test_non_curated_refused_for_camera(client, world):
    ws = await connect(client, world.tv_a)
    for bad in (world.camera_other, "camera.nope", world.light_a):
        data = await cmd(ws, 2, "perch/camera", camera_id=bad, kind="snapshot")
        assert data["error"]["code"] == "not_allowed"


async def test_bad_camera_request_is_invalid_format(client, world):
    ws = await connect(client, world.tv_a)
    data = await cmd(ws, 1, "perch/camera", camera_id=world.camera, kind="movie")
    assert data["error"]["code"] == "invalid_format"


async def test_unknown_and_reserved_types(client, world):
    ws = await connect(client, world.tv_a)
    await ws.send_json({"id": 1, "type": "perch/nope"})
    data = await recv(ws, "ErrorResultFrame")
    assert data["error"]["code"] == "unknown_command"
    await ws.send_json({"id": 2, "type": "config/auth/list"})
    assert (await recv(ws, "ErrorResultFrame"))["error"]["code"] == "unknown_command"
    await ws.send_json({"id": 3, "type": "perch/let_in", "sealed_pin": "AAAA"})
    assert (await recv(ws, "ErrorResultFrame"))["error"]["code"] == "not_supported_yet"


async def test_two_tvs_never_reach_each_other(client, world):
    a = await connect(client, world.tv_a)
    b = await connect(client, world.tv_b)
    # A tv_id in a frame is ignored by every command.
    for ws, tv, mine, theirs in (
        (a, "tv_a", world.light_a, world.light_b),
        (b, "tv_b", world.light_b, world.light_a),
    ):
        other = "tv_b" if tv == "tv_a" else "tv_a"
        manifest = (await cmd(ws, 1, "perch/manifest", tv_id=other))["result"]
        assert manifest["tv_id"] == tv
        assert [x["id"] for x in manifest["lights"]] == [mine]
        states = (await cmd(ws, 2, "perch/states", tv_id=other))["result"]
        assert [x["id"] for x in states["lights"]] == [mine]
        asked = await cmd(ws, 3, "perch/states", entity_ids=[theirs], tv_id=other)
        assert [x["id"] for x in asked["result"]["lights"]] == [mine]
        refused = await cmd(
            ws, 4, "perch/camera", camera_id=theirs, kind="snapshot", tv_id=other
        )
        assert refused["error"]["code"] == "not_allowed"


async def test_playback_without_room_answers_no_room(client, world):
    ws = await connect(client, world.tv_c)
    await ws.send_json(PLAYBACK)
    data = await recv(ws, "PlaybackResultFrame")
    assert data == {
        "id": 9, "type": "result", "success": True,
        "result": {"applied": False, "reason": "no_room"},
    }


async def test_playback_with_room_is_applied(hass, client, world):
    lights = await setup_lights(hass)
    ws = await connect(client, world.tv_a)
    await ws.send_json(PLAYBACK)
    data = await recv(ws, "PlaybackResultFrame")
    assert data["result"] == {"applied": True, "reason": None}
    assert lights.level(world.light_a) == ("on", 26)


async def test_playback_lighting_off_is_refused(client, world):
    ws = await connect(client, world.tv_a)
    await ws.send_json({**PLAYBACK, "lighting": False})
    data = await recv(ws, "PlaybackResultFrame")
    assert data["result"] == {"applied": False, "reason": "lighting_off"}


async def test_skip_tonight_then_playback_is_skipped(hass, client, world):
    ws = await connect(client, world.tv_a)
    until = (dt_util.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = await cmd(ws, 1, "perch/skip_tonight", until=until)
    assert data["result"] == {}
    await ws.send_json(PLAYBACK)
    data = await recv(ws, "PlaybackResultFrame")
    assert data["result"] == {"applied": False, "reason": "skipped_tonight"}


async def test_skip_tonight_rejects_bad_until(client, world):
    ws = await connect(client, world.tv_a)
    far = (dt_util.utcnow() + timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    past = (dt_util.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for n, until in enumerate((far, past, "tomorrow"), start=1):
        data = await cmd(ws, n, "perch/skip_tonight", until=until)
        assert data["error"]["code"] == "invalid_format"


async def test_playback_bad_fields_invalid_format(client, world):
    ws = await connect(client, world.tv_a)
    await ws.send_json({**PLAYBACK, "state": "dancing"})
    assert (await recv(ws, "ErrorResultFrame"))["error"]["code"] == "invalid_format"


# --- revoke -------------------------------------------------------------------


async def test_device_removal_revokes_open_socket_and_reconnect_fails(
    hass, client, world
):
    a = await connect(client, world.tv_a)
    b = await connect(client, world.tv_b)
    dev = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "tv_a"), world.entry.entry_id
    )
    assert dev is not None
    assert await async_remove_config_entry_device(hass, world.entry, dev)
    await hass.async_block_till_done()
    assert await recv(a, "RevokedFrame") == {"type": "revoked"}
    msg = await a.receive(timeout=5)
    assert msg.type is WSMsgType.CLOSE and msg.data == 4003
    # The other TV is untouched.
    assert (await cmd(b, 1, "perch/manifest"))["result"]["tv_id"] == "tv_b"
    ws = await client.ws_connect("/api/perch/ws")
    required = await recv(ws, "AuthRequiredFrame")
    await ws.send_json(world.tv_a.auth_frame(required))
    await expect_auth_invalid(ws)


async def test_subentry_removal_revokes(hass, client, world):
    a = await connect(client, world.tv_a)
    sub = next(
        s for s in world.entry.subentries.values() if s.data["tv_id"] == "tv_a"
    )
    hass.config_entries.async_remove_subentry(world.entry, sub.subentry_id)
    await hass.async_block_till_done()
    assert await recv(a, "RevokedFrame") == {"type": "revoked"}
    assert (await a.receive(timeout=5)).data == 4003


# --- logs ---------------------------------------------------------------------


async def test_no_secret_logged(client, world, caplog):
    caplog.set_level(logging.DEBUG)
    tv = world.tv_a
    ws = await client.ws_connect("/api/perch/ws")
    required = await recv(ws, "AuthRequiredFrame")
    frame = tv.auth_frame(required)
    await ws.send_json(frame)
    await recv(ws, "AuthOkFrame")
    await cmd(ws, 1, "perch/manifest")
    await cmd(ws, 2, "perch/camera", camera_id=world.camera, kind="snapshot")
    bad = await client.ws_connect("/api/perch/ws")
    req2 = await recv(bad, "AuthRequiredFrame")
    await bad.send_json(tv.auth_frame(req2, credential=os.urandom(32)))
    await expect_auth_invalid(bad)
    secrets = [
        tv.credential, base64.b64decode(frame["proof"]),
        base64.b64decode(required["challenge"]), base64.b64decode(req2["challenge"]),
    ]
    text = caplog.text
    for raw in secrets:
        assert raw.hex() not in text and b64(raw).decode() not in text
    assert tv.hash not in text
    assert frame["proof"] not in text and required["challenge"] not in text


# --- review fixes -------------------------------------------------------------


async def test_cloud_socket_closed_when_cloud_turned_off_local_stays(
    hass, client, world
):
    hass.config_entries.async_update_entry(
        world.entry, options={**world.entry.options, "allow_cloud": True}
    )
    await hass.async_block_till_done()
    with patch("custom_components.perch.access.is_cloud_connection", return_value=True):
        cloud = await connect(client, world.tv_a)
    local = await connect(client, world.tv_b)
    hass.config_entries.async_update_entry(
        world.entry, options={**world.entry.options, "allow_cloud": False}
    )
    await hass.async_block_till_done()
    msg = await cloud.receive(timeout=5)
    assert msg.type is WSMsgType.CLOSE and msg.data == 4003
    assert not local.closed
    assert (await cmd(local, 1, "perch/manifest"))["success"]


async def test_cloud_socket_refused_at_dispatch_when_cloud_is_off(
    hass, client, world
):
    hass.config_entries.async_update_entry(
        world.entry, options={**world.entry.options, "allow_cloud": True}
    )
    await hass.async_block_till_done()
    with patch("custom_components.perch.access.is_cloud_connection", return_value=True):
        cloud = await connect(client, world.tv_a)
        # change the option without the listener closing the socket
        with patch("custom_components.perch.close_cloud_sockets"):
            hass.config_entries.async_update_entry(
                world.entry, options={**world.entry.options, "allow_cloud": False}
            )
            await hass.async_block_till_done()
        await cloud.send_json({"id": 1, "type": "ping"})
        msg = await cloud.receive(timeout=5)
    assert msg.type is WSMsgType.CLOSE and msg.data == 4003


async def test_unload_during_auth_stops_the_connection(hass, client, world):
    ws = await client.ws_connect("/api/perch/ws")
    required = await recv(ws, "AuthRequiredFrame")
    await hass.config_entries.async_unload(world.entry.entry_id)
    await ws.send_json(world.tv_a.auth_frame(required))
    msg = await ws.receive(timeout=5)
    assert msg.type is WSMsgType.TEXT and msg.json() == {"type": "auth_invalid"}
    msg = await ws.receive(timeout=5)
    assert msg.type is WSMsgType.CLOSE


async def test_send_after_revoke_does_not_raise(hass, client, world):
    ws = await connect(client, world.tv_a)
    original = web.WebSocketResponse.send_json

    async def closed_send(self, *args, **kwargs):
        raise ConnectionResetError("closed")

    with patch.object(web.WebSocketResponse, "send_json", closed_send):
        await ws.send_json({"id": 1, "type": "ping"})
        await ws.receive(timeout=5)
    assert original is not closed_send
