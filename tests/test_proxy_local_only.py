"""The local-only check, end to end behind Home Assistant's proxy handling.

Nothing here patches `is_local`: Home Assistant's `http` integration works out
the client address from `X-Forwarded-For` (or not), and the companion judges
what it is given. The test client connects from 127.0.0.1.
"""

from __future__ import annotations

from aiohttp.client_exceptions import WSServerHandshakeError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.setup import async_setup_component

from custom_components.perch.const import DOMAIN

PUBLIC = "203.0.113.7"  # documentation range (TEST-NET-3)
PRIVATE = "10.0.0.5"  # RFC1918

TRUSTED_PROXY = {"use_x_forwarded_for": True, "trusted_proxies": ["127.0.0.1"]}


async def _client(hass, hass_client_no_auth, http_config):
    assert await async_setup_component(hass, "http", {"http": http_config})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return await hass_client_no_auth()


async def _pair(client, forwarded=None):
    headers = {"X-Forwarded-For": forwarded} if forwarded else {}
    # An empty body: a request that passes the access gate is then answered
    # 400 bad_request, which tells "let in" apart from "not_local".
    resp = await client.post("/api/perch/pair", json={}, headers=headers)
    return resp.status, await resp.json()


async def _ws_status(client, forwarded=None):
    headers = {"X-Forwarded-For": forwarded} if forwarded else {}
    try:
        ws = await client.ws_connect("/api/perch/ws", headers=headers)
    except WSServerHandshakeError as err:
        return err.status
    await ws.close()
    return 101


async def test_proxied_public_client_refused_for_pairing(hass, hass_client_no_auth):
    client = await _client(hass, hass_client_no_auth, TRUSTED_PROXY)
    assert await _pair(client, PUBLIC) == (403, {"error": "not_local"})
    resp = await client.get("/api/perch/pair/abc", headers={"X-Forwarded-For": PUBLIC})
    assert resp.status == 403 and await resp.json() == {"error": "not_local"}


async def test_proxied_public_client_refused_for_websocket(hass, hass_client_no_auth):
    client = await _client(hass, hass_client_no_auth, TRUSTED_PROXY)
    assert await _ws_status(client, PUBLIC) == 403


async def test_proxied_private_client_allowed(hass, hass_client_no_auth):
    client = await _client(hass, hass_client_no_auth, TRUSTED_PROXY)
    assert await _pair(client, PRIVATE) == (400, {"error": "bad_request"})
    assert await _ws_status(client, PRIVATE) == 101


async def test_direct_local_request_allowed_without_forwarding(
    hass, hass_client_no_auth
):
    client = await _client(hass, hass_client_no_auth, {})
    assert await _pair(client) == (400, {"error": "bad_request"})
    assert await _ws_status(client) == 101


async def test_caveat_proxy_that_hides_the_visitor_makes_everyone_look_local(
    hass, hass_client_no_auth
):
    """Documented caveat: a proxy that connects locally and sends no
    X-Forwarded-For (or one Home Assistant is not set up to read) hides the
    visitor, so a public visitor behind it passes the local-only check."""
    client = await _client(hass, hass_client_no_auth, TRUSTED_PROXY)
    assert await _pair(client) == (400, {"error": "bad_request"})
    assert await _ws_status(client) == 101


async def test_forwarded_header_without_trusted_config_is_refused_by_home_assistant(
    hass, hass_client_no_auth
):
    """Home Assistant itself answers 400 before the companion is reached."""
    client = await _client(hass, hass_client_no_auth, {})
    resp = await client.post(
        "/api/perch/pair", json={}, headers={"X-Forwarded-For": PUBLIC}
    )
    assert resp.status == 400
    assert "bad_request" not in await resp.text()
