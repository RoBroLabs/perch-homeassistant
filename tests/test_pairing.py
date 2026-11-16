"""TV pairing: endpoint, access, subentry flow and log hygiene.

Every JSON reply the views give is validated against the protocol schema by the
`post` and `poll` helpers, so each status and error shape is checked as it is
produced.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import logging
import os
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.perch import crypto
from custom_components.perch.const import DOMAIN
from custom_components.perch.pairing import Contender, PairingWindow
from custom_components.perch.words import async_get_words

from .schema import validate
from .test_crypto import _open

CODE = "482916"
b64 = base64.b64encode


_sources = itertools.count(1)


class Tv:
    """A TV with a key pair, a nonce and a binding for a code. Each TV posts
    from its own address unless a test says otherwise."""

    def __init__(self, name="Lounge", code=CODE):
        self.name = name
        self.source = f"10.1.0.{next(_sources)}"
        self.priv = X25519PrivateKey.generate()
        self.pub = self.priv.public_key().public_bytes_raw()
        self.nonce = os.urandom(16)
        self.mac = crypto.binding_hmac(code, self.pub, self.nonce)
        self.pairing_id = None

    def body(self, **over):
        body = {
            "protocol": 1,
            "tv_name": self.name,
            "public_key": b64(self.pub).decode(),
            "nonce": b64(self.nonce).decode(),
            "hmac": b64(self.mac).decode(),
        }
        body.update(over)
        return body


class Clock:
    """A movable clock for both modules that read `_now`."""

    def __init__(self):
        self.t = dt_util.utcnow().timestamp()

    def tick(self, seconds):
        self.t += seconds

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    c = Clock()
    with (
        patch("custom_components.perch.pairing._now", c),
        patch("custom_components.perch.views._now", c),
    ):
        yield c


@pytest.fixture
async def entry(hass):
    assert await async_setup_component(hass, "http", {})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
async def client(entry, hass_client_no_auth, clock):
    return await hass_client_no_auth()


async def post(client, tv_or_body, *, headers=False, source=None):
    body = tv_or_body.body() if isinstance(tv_or_body, Tv) else tv_or_body
    source = tv_or_body.source if isinstance(tv_or_body, Tv) else source or "10.1.9.9"
    with patch("custom_components.perch.views._source", return_value=source):
        resp = await client.post("/api/perch/pair", json=body)
    data = await resp.json()
    validate("PairStartResponse" if resp.status == 200 else "PairError", data)
    if isinstance(tv_or_body, Tv) and resp.status == 200:
        tv_or_body.pairing_id = data["pairing_id"]
    return (resp.status, data, resp.headers) if headers else (resp.status, data)


async def poll(client, tv):
    resp = await client.get(f"/api/perch/pair/{tv.pairing_id}")
    data = await resp.json()
    assert resp.status == 200
    validate("PairStatusResponse", data)
    return data


async def start_flow(hass, entry):
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, "tv"), context={"source": "user"}
    )


async def enter(hass, flow, code):
    return await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"code": code}
    )


async def pick(hass, flow, option):
    return await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"next_step_id": option}
    )


def tv_subentries(entry):
    return [s for s in entry.subentries.values() if s.subentry_type == "tv"]


async def pair_fully(hass, entry, client, tv):
    await post(client, tv)
    flow = await start_flow(hass, entry)
    res = await enter(hass, flow, CODE)
    assert res["step_id"] == "confirm"
    return await pick(hass, res, "match")


# --- start endpoint ---------------------------------------------------------


async def test_start_reply_shape(client):
    status, data = await post(client, Tv())
    assert status == 200
    assert set(data) == {"pairing_id", "expires_in"}
    assert isinstance(data["expires_in"], int) and 1 <= data["expires_in"] <= 300
    assert data["expires_in"] == 300


async def test_late_joiner_gets_remaining_seconds(client, clock):
    assert (await post(client, Tv("A")))[1]["expires_in"] == 300
    clock.tick(113)
    status, data = await post(client, Tv("B"))
    assert status == 200 and data["expires_in"] == 187
    clock.tick(186.5)
    assert (await post(client, Tv("C")))[1]["expires_in"] == 1


async def test_retry_of_same_request_returns_same_pairing(client):
    tv = Tv()
    first = await post(client, tv)
    assert await post(client, tv) == first


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(public_key="!!!notbase64"),
        lambda b: b.update(nonce=b64(b"x" * 15).decode()),
        lambda b: b.update(public_key=b64(b"x" * 31).decode()),
        lambda b: b.update(hmac=b64(b"x" * 33).decode()),
        lambda b: b.update(hmac=5),
        lambda b: b.pop("hmac"),
        lambda b: b.pop("nonce"),
        lambda b: b.pop("public_key"),
        lambda b: b.pop("tv_name"),
        lambda b: b.pop("protocol"),
        lambda b: b.update(protocol="1"),
        lambda b: b.update(protocol=True),
        lambda b: b.update(tv_name=""),
        lambda b: b.update(tv_name="   "),
        lambda b: b.update(tv_name="x" * 65),
        lambda b: b.update(tv_name=5),
    ],
)
async def test_bad_request(client, mutate):
    body = Tv().body()
    mutate(body)
    status, data = await post(client, body)
    assert (status, data) == (400, {"error": "bad_request"})


async def test_tv_name_limits_accepted(client):
    assert (await post(client, Tv("x")))[0] == 200


async def test_non_object_body_is_bad_request(client):
    assert await post(client, ["x"]) == (400, {"error": "bad_request"})


async def test_unparseable_body_is_bad_request(client):
    resp = await client.post("/api/perch/pair", data="not json")
    assert resp.status == 400 and await resp.json() == {"error": "bad_request"}


async def test_unsupported_protocol(client):
    body = Tv().body(protocol=2)
    assert await post(client, body) == (400, {"error": "unsupported_protocol"})


async def test_busy_when_four_contenders(client):
    for i in range(4):
        assert (await post(client, Tv(f"T{i}")))[0] == 200
    assert await post(client, Tv("fifth")) == (409, {"error": "busy"})


async def test_busy_when_same_key_new_nonce(client):
    tv = Tv()
    assert (await post(client, tv))[0] == 200
    nonce = os.urandom(16)
    mac = crypto.binding_hmac(CODE, tv.pub, nonce)
    body = tv.body(nonce=b64(nonce).decode(), hmac=b64(mac).decode())
    assert await post(client, body) == (409, {"error": "busy"})


async def test_not_found_ids(client):
    for pid in ("nope", "%C3%A9%C3%A9"):
        resp = await client.get(f"/api/perch/pair/{pid}")
        assert resp.status == 404
        data = await resp.json()
        assert data == {"error": "not_found"}
        validate("PairError", data)


async def test_not_found_when_unloaded(hass, entry, client):
    tv = Tv()
    await post(client, tv)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await post(client, Tv()) == (404, {"error": "not_found"})
    resp = await client.get(f"/api/perch/pair/{tv.pairing_id}")
    assert resp.status == 404


# --- access and throttle ----------------------------------------------------


async def test_not_local_public_address(client):
    with patch("custom_components.perch.access.is_local", return_value=False):
        assert await post(client, Tv()) == (403, {"error": "not_local"})
        resp = await client.get("/api/perch/pair/abc")
    assert resp.status == 403 and await resp.json() == {"error": "not_local"}


async def test_cloud_request_is_pairing_local_only(client):
    with patch("custom_components.perch.access.is_cloud_connection", return_value=True):
        assert await post(client, Tv()) == (
            403,
            {"error": "pairing_local_only"},
        )
        resp = await client.get("/api/perch/pair/abc")
    assert resp.status == 403
    assert await resp.json() == {"error": "pairing_local_only"}


async def test_cloud_refused_even_from_non_local_address(client):
    with (
        patch("custom_components.perch.access.is_cloud_connection", return_value=True),
        patch("custom_components.perch.access.is_local", return_value=False),
    ):
        assert (await post(client, Tv()))[1] == {"error": "pairing_local_only"}


async def test_pair_rate_limit_ten_a_minute(client, clock):
    for i in range(10):
        assert (await post(client, Tv(f"T{i}")))[0] in (200, 409)
    status, data, headers = await post(client, Tv(), headers=True)
    assert status == 429
    assert data["error"] == "rate_limited"
    assert isinstance(data["retry_after"], int) and data["retry_after"] >= 1
    assert headers["Retry-After"] == str(data["retry_after"])
    # still blocked a little later, and open again after the back-off
    clock.tick(10)
    assert (await post(client, Tv()))[0] == 429
    clock.tick(data["retry_after"] + 1)
    assert (await post(client, Tv()))[0] in (200, 409)


async def test_throttled_request_is_not_processed(client):
    for i in range(10):
        await post(client, Tv(f"T{i}"))
    late = Tv("late")
    assert (await post(client, late))[0] == 429
    assert late.pairing_id is None


async def test_rate_limit_does_not_apply_to_ten_requests(client):
    for i in range(10):
        assert (await post(client, Tv(f"T{i}")))[0] != 429


# --- subentry flow ----------------------------------------------------------


async def test_happy_path(hass, entry, client):
    tv = Tv()
    await post(client, tv)
    assert await poll(client, tv) == {"status": "pending"}

    flow = await start_flow(hass, entry)
    assert flow["step_id"] == "user"
    res = await enter(hass, flow, CODE)
    assert res["type"] == "menu" and res["step_id"] == "confirm"
    expected = crypto.fingerprint_words(tv.pub, await async_get_words(hass))
    assert len(expected) == 4
    assert res["description_placeholders"]["words"] == " ".join(expected)
    assert res["menu_options"] == ["match", "mismatch"]
    # nothing is created, and the TV still waits, until the admin confirms
    assert tv_subentries(entry) == []
    assert await poll(client, tv) == {"status": "pending"}

    res = await pick(hass, res, "match")
    assert res["type"] == "create_entry"
    subs = tv_subentries(entry)
    assert len(subs) == 1
    sub = subs[0]
    assert sub.data["tv_id"].startswith("tv_")
    assert sub.data["public_key"] == b64(tv.pub).decode()
    assert set(sub.data) == {"tv_id", "public_key", "credential_hash"}

    polled = await poll(client, tv)
    assert polled["status"] == "approved"
    assert polled["tv_id"] == sub.data["tv_id"]
    cred = _open(base64.b64decode(polled["envelope"]), tv.priv, tv.nonce)
    assert len(cred) == 32
    assert hashlib.sha256(cred).hexdigest() == sub.data["credential_hash"]
    # the credential itself is stored nowhere
    assert cred.hex() not in repr(dict(sub.data)) and b64(cred).decode() not in repr(
        dict(sub.data)
    )


async def test_creates_device_for_the_tv(hass, entry, client):
    await pair_fully(hass, entry, client, Tv())
    sub = tv_subentries(entry)[0]
    dev = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, sub.data["tv_id"]), entry.entry_id
    )
    assert dev is not None


async def test_wrong_code_then_right(hass, entry, client):
    tv = Tv()
    await post(client, tv)
    flow = await start_flow(hass, entry)
    res = await enter(hass, flow, "000000")
    assert res["type"] == "form" and res["errors"] == {"code": "invalid_code"}
    assert tv_subentries(entry) == []
    res = await enter(hass, flow, CODE)
    assert res["step_id"] == "confirm"


async def test_fifth_wrong_code_aborts_and_sixth_is_refused(hass, entry, client):
    tv = Tv()
    await post(client, tv)
    flow = await start_flow(hass, entry)
    second = await start_flow(hass, entry)  # another admin tab on the same window
    for _ in range(4):
        res = await enter(hass, flow, "000000")
        assert res["errors"] == {"code": "invalid_code"}
    res = await enter(hass, flow, "000000")
    assert res["type"] == "abort" and res["reason"] == "too_many_attempts"
    assert (await poll(client, tv))["status"] == "cancelled"

    # a further entry, even the right code, is refused
    res = await enter(hass, second, CODE)
    assert res["type"] == "abort" and res["reason"] == "no_pending"
    flow3 = await start_flow(hass, entry)
    assert flow3["type"] == "abort" and flow3["reason"] == "no_pending"
    assert tv_subentries(entry) == []
    assert (await poll(client, tv))["status"] == "cancelled"


async def test_attempts_option_three(hass, entry, client):
    hass.config_entries.async_update_entry(entry, options={"pairing_attempts": 3})
    tv = Tv()
    await post(client, tv)
    flow = await start_flow(hass, entry)
    for _ in range(2):
        assert (await enter(hass, flow, "000000"))["type"] == "form"
    res = await enter(hass, flow, "000000")
    assert res["type"] == "abort" and res["reason"] == "too_many_attempts"
    assert (await poll(client, tv))["status"] == "cancelled"


async def test_swapped_key_never_matches_right_code(hass, entry, client):
    a, b = Tv(), Tv()
    # a's HMAC, made for a's key, posted with b's public key
    status, _ = await post(client, a.body(public_key=b64(b.pub).decode()))
    assert status == 200
    flow = await start_flow(hass, entry)
    res = await enter(hass, flow, CODE)
    assert res["type"] == "form" and res["errors"] == {"code": "invalid_code"}
    assert tv_subentries(entry) == []


async def test_two_valid_keys_abort(hass, entry, client):
    a, b = Tv("A"), Tv("B")
    await post(client, a)
    await post(client, b)
    flow = await start_flow(hass, entry)
    res = await enter(hass, flow, CODE)
    assert res["type"] == "abort" and res["reason"] == "aborted"
    assert (await poll(client, a))["status"] == "cancelled"
    assert (await poll(client, b))["status"] == "cancelled"
    assert tv_subentries(entry) == []

    from homeassistant.components import persistent_notification as pn

    notes = pn._async_get_or_create_notifications(hass)
    assert len(notes) == 1 and "perch" in next(iter(notes))


async def test_invalid_hmac_contender_does_not_block_valid(hass, entry, client):
    good, bad = Tv("Good"), Tv("Bad", code="999999")
    await post(client, bad)
    await post(client, good)
    res = await pair_fully_after_posts(hass, entry)
    assert res["type"] == "create_entry"
    assert (await poll(client, good))["status"] == "approved"
    assert (await poll(client, bad))["status"] == "cancelled"
    assert len(tv_subentries(entry)) == 1


async def pair_fully_after_posts(hass, entry):
    flow = await start_flow(hass, entry)
    res = await enter(hass, flow, CODE)
    assert res["step_id"] == "confirm"
    return await pick(hass, res, "match")


async def test_mismatch_rejects(hass, entry, client):
    tv = Tv()
    await post(client, tv)
    flow = await start_flow(hass, entry)
    res = await enter(hass, flow, CODE)
    res = await pick(hass, res, "mismatch")
    assert res["type"] == "abort" and res["reason"] == "mismatch"
    assert (await poll(client, tv))["status"] == "rejected"
    assert tv_subentries(entry) == []


async def test_poll_says_expired_after_window(hass, entry, client, clock):
    tv = Tv()
    await post(client, tv)
    flow = await start_flow(hass, entry)
    clock.tick(299)
    assert (await poll(client, tv))["status"] == "pending"
    clock.tick(2)
    assert (await poll(client, tv))["status"] == "expired"
    res = await enter(hass, flow, CODE)
    assert res["type"] == "abort" and res["reason"] == "no_pending"
    assert tv_subentries(entry) == []
    assert (await start_flow(hass, entry))["reason"] == "no_pending"


async def test_flow_without_pending_pairing_aborts(hass, entry):
    res = await start_flow(hass, entry)
    assert res["type"] == "abort" and res["reason"] == "no_pending"


async def test_new_window_after_expiry(client, clock):
    assert (await post(client, Tv("A")))[1]["expires_in"] == 300
    clock.tick(301)
    status, data = await post(client, Tv("B"))
    assert status == 200 and data["expires_in"] == 300


async def test_device_removal_removes_subentry(hass, entry, client):
    await pair_fully(hass, entry, client, Tv())
    sub = tv_subentries(entry)[0]
    reg = dr.async_get(hass)
    dev = reg.async_get_device_by_identifier(
        (DOMAIN, sub.data["tv_id"]), entry.entry_id
    )
    assert dev is not None

    from custom_components.perch import async_remove_config_entry_device

    assert await async_remove_config_entry_device(hass, entry, dev)
    await hass.async_block_till_done()
    assert tv_subentries(entry) == []
    assert reg.async_get_device_by_identifier(
        (DOMAIN, sub.data["tv_id"]), entry.entry_id
    ) is None


# --- log hygiene ------------------------------------------------------------


async def test_no_secret_logged(hass, entry, client, caplog):
    caplog.set_level(logging.DEBUG, logger="")
    tv = Tv()
    other = Tv("Other", code="999999")
    await post(client, other)
    await post(client, tv)
    flow = await start_flow(hass, entry)
    await enter(hass, flow, "000000")
    res = await enter(hass, flow, CODE)
    await pick(hass, res, "match")
    polled = await poll(client, tv)
    assert polled["status"] == "approved"
    envelope = base64.b64decode(polled["envelope"])
    cred = _open(envelope, tv.priv, tv.nonce)
    sub = tv_subentries(entry)[0]

    secrets_ = [
        CODE,
        tv.mac.hex(),
        b64(tv.mac).decode(),
        polled["envelope"],
        envelope.hex(),
        b64(cred).decode(),
        cred.hex(),
        sub.data["credential_hash"],
        b64(tv.priv.private_bytes_raw()).decode(),
        tv.priv.private_bytes_raw().hex(),
    ]
    text = caplog.text
    for secret in secrets_:
        assert secret not in text

    contender = Contender("x", tv.pub, tv.nonce, tv.mac, envelope=envelope)
    window = PairingWindow(contenders=[contender])
    for obj in (contender, window):
        r = repr(obj)
        for raw in (tv.pub, tv.nonce, tv.mac, envelope):
            assert raw.hex() not in r and b64(raw).decode() not in r
            assert repr(raw) not in r


# --- review fixes -------------------------------------------------------------


async def test_one_address_cannot_fill_the_window(client):
    a, b = Tv("A"), Tv("B")
    b.source = a.source
    assert (await post(client, a))[0] == 200
    assert await post(client, b) == (409, {"error": "busy"})
    # a retry of the same request from that address still gets the same pairing
    status, data = await post(client, a.body(), source=a.source)
    assert status == 200 and data["pairing_id"] == a.pairing_id
    # another address is not affected
    assert (await post(client, Tv("C")))[0] == 200


async def test_approved_tv_still_gets_its_envelope_after_a_new_post(
    hass, entry, client
):
    first = Tv("First")
    await post(client, first)
    res = await pair_fully_after_posts(hass, entry)
    assert res["type"] == "create_entry"
    # a new window opens before the first TV polls
    second = Tv("Second")
    assert (await post(client, second))[0] == 200
    polled = await poll(client, first)
    assert polled["status"] == "approved" and polled["envelope"]
    assert await poll(client, second) == {"status": "pending"}
    # approved is told once, then the pairing is gone
    resp = await client.get(f"/api/perch/pair/{first.pairing_id}")
    assert resp.status == 404


async def test_approved_contender_lapses_after_pairing_ttl(hass, entry, client, clock):
    tv = Tv()
    await post(client, tv)
    await pair_fully_after_posts(hass, entry)
    clock.tick(301)
    resp = await client.get(f"/api/perch/pair/{tv.pairing_id}")
    assert resp.status == 404


async def test_tv_name_is_plain_text_in_the_admin_flow(hass, entry, client):
    tv = Tv("[x](http://evil) **bold** # <b>`|!_")
    await post(client, tv)
    flow = await start_flow(hass, entry)
    assert flow["step_id"] == "user"
    shown = flow["description_placeholders"]["tv_name"]
    assert not set(shown) & set("[]()*_`<>#!|\r\n")
    res = await enter(hass, flow, CODE)
    assert res["description_placeholders"]["tv_name"] == shown


def test_plain_strips_markdown_and_newlines():
    from custom_components.perch.config_flow import _plain

    assert _plain("[a](b)\n*c*_`d`<e>#f!g|h\r") == "abcdefgh"


def _limiter(per_source=100, overall=100_000):
    from custom_components.perch.pairing import RateLimiter

    return RateLimiter(per_source, overall)


def test_ipv6_rotation_within_a_64_is_one_source(clock) -> None:
    # Global addresses share a /64; the documentation prefix is treated as global.
    limiter = _limiter(per_source=3)
    results = [limiter.check(f"2001:db8:1:2::{i:x}") for i in range(1, 6)]
    assert results[:3] == [0, 0, 0]
    assert all(results[3:])
    assert len(limiter._sources) == 1
    assert limiter.check("2001:db8:1:3::1") == 0


def test_ipv4_per_address_and_mapped_ipv6_is_ipv4(clock) -> None:
    limiter = _limiter(per_source=1)
    assert limiter.check("10.0.0.1") == 0
    assert limiter.check("10.0.0.1") > 0
    assert limiter.check("10.0.0.2") == 0
    assert limiter.check("::ffff:10.0.0.2") > 0
    assert limiter.check("not-an-address") == 0
    assert limiter.check("not-an-address") > 0
    assert len(limiter._sources) == 3


def test_overall_lock_does_not_grow_source_map(clock) -> None:
    limiter = _limiter(overall=2)
    assert limiter.check("10.0.0.1") == 0
    assert limiter.check("10.0.0.2") == 0
    assert limiter.check("10.0.0.3") > 0
    size = len(limiter._sources)
    for i in range(50):
        assert limiter.check(f"10.1.0.{i}") > 0
    assert len(limiter._sources) == size


def test_source_map_is_capped_and_evicts_oldest(clock) -> None:
    from custom_components.perch.pairing import MAX_SOURCES

    limiter = _limiter()
    for i in range(MAX_SOURCES + 10):
        limiter.check(f"10.{i // 256}.{i % 256}.1")
    assert len(limiter._sources) == MAX_SOURCES
    assert "10.0.0.1" not in limiter._sources
    last = MAX_SOURCES + 9
    assert f"10.{last // 256}.{last % 256}.1" in limiter._sources


def test_private_and_link_local_ipv6_are_per_address(clock) -> None:
    limiter = _limiter(per_source=1)
    for prefix in ("fd12:3456:789a:1::", "fe80::", "::"):
        assert limiter.check(f"{prefix}1") == 0
        assert limiter.check(f"{prefix}2") == 0
        assert limiter.check(f"{prefix}1") > 0


def test_backoff_keeps_doubling_across_a_prune(clock) -> None:
    limiter = _limiter(per_source=1)
    assert limiter.check("10.0.0.1") == 0
    assert limiter.check("10.0.0.1") == 60
    clock.tick(60)
    # A new key triggers a prune while the penalty is still remembered.
    assert limiter.check("10.0.0.2") == 0
    assert limiter.check("10.0.0.1") == 0
    assert limiter.check("10.0.0.1") == 120


def test_eviction_is_least_recently_used(clock) -> None:
    from custom_components.perch.pairing import MAX_SOURCES

    limiter = _limiter()
    for i in range(MAX_SOURCES):
        limiter.check(f"10.{i // 256}.{i % 256}.1")
    limiter.check("10.0.0.1")  # refresh the oldest
    limiter.check("192.168.9.9")  # forces one eviction
    assert "10.0.0.1" in limiter._sources
    assert "10.0.1.1" not in limiter._sources
