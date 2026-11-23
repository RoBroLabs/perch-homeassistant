"""`perch/let_in`: every refusal, success, lockout, restarts, logbook and logs."""

from __future__ import annotations

import asyncio
import base64
from datetime import timedelta
import logging

import pytest

from homeassistant.core import ServiceCall, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.perch import crypto
from custom_components.perch.runtime import get_runtime

from .test_endpoint import client, recv, world  # noqa: F401 - fixtures
from .schema import validate

PIN = "13572468"
WRONG = "86427531"
BELL = "binary_sensor.bell"
SCRIPT = "script.let_in"


class Door:
    """A script that calls a fake `test.unlock`; `opened` lists its contexts."""

    def __init__(self) -> None:
        self.opened: list = []
        self.fail = False

    async def unlock(self, call: ServiceCall) -> None:
        if self.fail:
            raise RuntimeError("lock jammed")
        self.opened.append(call.context)


@pytest.fixture
async def house(hass, world):
    door = Door()
    hass.services.async_register("test", "unlock", door.unlock)
    assert await async_setup_component(
        hass, "script", {"script": {"let_in": {"sequence": [{"action": "test.unlock"}]}}}
    )
    hass.states.async_set(BELL, "off")
    phone: list[str] = []

    async def notify(call: ServiceCall) -> None:
        phone.append(call.data["message"])

    hass.services.async_register("notify", "mobile_app_robin", notify)
    pin_hash = await hass.async_add_executor_job(crypto.hash_pin, PIN)
    entry = world.entry
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "doorbell": BELL,
            "let_in_enabled": True,
            "let_in_script": SCRIPT,
            "let_in_pin_hash": pin_hash,
            "notify_services": ["notify.mobile_app_robin"],
        },
    )
    sub = next(s for s in entry.subentries.values() if s.data["tv_id"] == "tv_a")
    hass.config_entries.async_update_subentry(
        entry, sub, data={**sub.data, "let_in_allowed": True}
    )
    await hass.async_block_till_done()
    logbook: list[dict] = []
    hass.bus.async_listen(
        "logbook_entry", callback(lambda e: logbook.append(dict(e.data)))
    )
    world.door, world.phone, world.logbook = door, phone, logbook
    return world


class Link:
    """One authenticated connection that can seal PINs for it."""

    def __init__(self, ws, tv, required) -> None:
        self.ws, self.tv = ws, tv
        self.server_key = base64.b64decode(required["server_key"])
        self.challenge = base64.b64decode(required["challenge"])
        self.cid = 0

    def seal(self, pin, cid, credential=None):
        return base64.b64encode(
            crypto.seal_pin(
                pin, self.server_key, credential or self.tv.credential,
                self.challenge, self.tv.priv, cid,
            )
        ).decode()

    async def let_in(self, pin=PIN, *, seal_cid=None, sealed=None, **extra):
        self.cid += 1
        cid = self.cid
        sealed = sealed or self.seal(pin, seal_cid or cid)
        await self.ws.send_json(
            {"id": cid, "type": "perch/let_in", "sealed_pin": sealed, **extra}
        )
        data = await recv(self.ws)
        assert data["id"] == cid
        validate("LetInResultFrame" if data["success"] else "ErrorResultFrame", data)
        return data

    async def refusal(self, pin=PIN, **kw):
        data = await self.let_in(pin, **kw)
        assert data["success"], data
        return data["result"]["refusal"]

    async def manifest(self):
        self.cid += 1
        await self.ws.send_json({"id": self.cid, "type": "perch/manifest"})
        data = await recv(self.ws, "ManifestResultFrame")
        return data["result"]


async def link(client, tv):
    ws = await client.ws_connect("/api/perch/ws")
    required = await recv(ws, "AuthRequiredFrame")
    await ws.send_json(tv.auth_frame(required))
    await recv(ws, "AuthOkFrame")
    return Link(ws, tv, required)


async def ring(hass):
    hass.states.async_set(BELL, "on")
    await hass.async_block_till_done()
    hass.states.async_set(BELL, "off")
    await hass.async_block_till_done()


def lock(hass):
    return get_runtime(hass).let_in.state


def end_lock(hass):
    lock(hass).locked_until = dt_util.utcnow().timestamp() - 1


async def reload(hass, entry):
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()


# --- success and the ring --------------------------------------------------------


async def test_opens_with_a_ring_and_the_pin(hass, client, house):
    tv = await link(client, house.tv_a)
    assert (await tv.manifest())["let_in_available"] is True
    await ring(hass)
    data = await tv.let_in()
    assert data["result"] == {"opened": True, "refusal": None}
    await hass.async_block_till_done()
    assert len(house.door.opened) == 1
    assert house.logbook[-1]["message"] == "Lounge TV let someone in"
    assert house.logbook[-1]["entity_id"] == SCRIPT


async def test_no_ring_old_ring_and_used_ring(hass, client, house):
    tv = await link(client, house.tv_a)
    assert await tv.refusal() == "no_ring"
    get_runtime(hass).hub.last_ring = dt_util.utcnow() - timedelta(minutes=2, seconds=1)
    assert await tv.refusal() == "no_ring"
    get_runtime(hass).hub.last_ring = dt_util.utcnow() - timedelta(minutes=1, seconds=50)
    assert (await tv.let_in())["result"]["opened"] is True
    # The ring is used up, even from another TV's connection.
    assert await tv.refusal() == "no_ring"
    await ring(hass)
    assert (await tv.let_in())["result"]["opened"] is True
    assert len(house.door.opened) == 2
    assert lock(hass).failures == 0


async def test_ring_from_the_future_is_not_a_ring(hass, client, house):
    tv = await link(client, house.tv_a)
    get_runtime(hass).hub.last_ring = dt_util.utcnow() + timedelta(minutes=1)
    assert await tv.refusal() == "no_ring"


async def test_restart_forgets_the_ring(hass, client, house):
    await ring(hass)
    await reload(hass, house.entry)
    tv = await link(client, house.tv_a)
    assert await tv.refusal() == "no_ring"
    assert house.door.opened == []


async def test_not_allowed_tv(hass, client, house):
    tv = await link(client, house.tv_b)
    assert (await tv.manifest())["let_in_available"] is False
    await ring(hass)
    assert await tv.refusal() == "not_allowed"
    assert house.logbook[-1]["message"] == (
        "Kitchen TV was refused: this TV is not allowed to let people in"
    )
    assert house.door.opened == []


@pytest.mark.parametrize(
    "change",
    [
        {"let_in_enabled": False},
        {"let_in_script": None},
        {"let_in_pin_hash": None},
        {"doorbell": None},
    ],
)
async def test_disabled_until_fully_set_up(hass, client, house, change):
    options = {k: v for k, v in house.entry.options.items() if change.get(k, 1) is not None}
    options.update({k: v for k, v in change.items() if v is not None})
    hass.config_entries.async_update_entry(house.entry, options=options)
    await hass.async_block_till_done()
    tv = await link(client, house.tv_a)
    assert (await tv.manifest())["let_in_available"] is False
    get_runtime(hass).hub.last_ring = dt_util.utcnow()
    assert await tv.refusal() == "disabled"
    assert house.door.opened == []


async def test_role_claim_makes_no_difference(hass, client, house):
    tv = await link(client, house.tv_a)
    await ring(hass)
    assert await tv.refusal(WRONG, role="adult", admin_pin_fresh=True) == "wrong_pin"
    assert (await tv.let_in(role="kid"))["result"]["opened"] is True
    tv_b = await link(client, house.tv_b)
    await ring(hass)
    assert await tv_b.refusal(role="adult", allowed=True) == "not_allowed"


# --- lockout ---------------------------------------------------------------------


async def test_lockout_escalates_then_disables_and_admin_re_enables(hass, client, house):
    tv = await link(client, house.tv_a)
    await ring(hass)
    assert await tv.refusal(WRONG) == "wrong_pin"
    assert await tv.refusal(WRONG) == "wrong_pin"
    assert await tv.refusal(WRONG) == "locked_out"
    await hass.async_block_till_done()
    until = lock(hass).locked_until - dt_util.utcnow().timestamp()
    assert 14 * 60 < until <= 15 * 60
    assert "locked for 15 minutes" in house.phone[-1]
    assert (await tv.manifest())["let_in_available"] is False
    # Even the right PIN is refused while locked.
    assert await tv.refusal() == "locked_out"
    assert house.door.opened == []

    end_lock(hass)
    for _ in range(2):
        assert await tv.refusal(WRONG) == "wrong_pin"
    assert await tv.refusal(WRONG) == "locked_out"
    await hass.async_block_till_done()
    until = lock(hass).locked_until - dt_util.utcnow().timestamp()
    assert 59 * 60 < until <= 60 * 60
    assert "locked for 1 hour" in house.phone[-1]

    end_lock(hass)
    for _ in range(2):
        assert await tv.refusal(WRONG) == "wrong_pin"
    assert await tv.refusal(WRONG) == "disabled"
    await hass.async_block_till_done()
    assert "now off" in house.phone[-1]
    assert len(house.phone) == 3
    assert await tv.refusal() == "disabled"
    assert house.door.opened == []

    # The options form shows it off; switching it on is the re-enable.
    flow = await hass.config_entries.options.async_init(house.entry.entry_id)
    let_in = next(v for k, v in flow["data_schema"].schema.items() if k == "let_in")
    default = next(k for k in let_in.schema.schema if k == "let_in_enabled").default()
    assert default is False
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {"advanced": {}, "doorbell": BELL,
         "let_in": {"let_in_enabled": True, "let_in_script": SCRIPT}},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert lock(hass).level == 0 and not lock(hass).disabled
    tv = await link(client, house.tv_a)
    assert (await tv.let_in())["result"]["opened"] is True


async def test_success_resets_wrong_count_not_the_level(hass, client, house):
    tv = await link(client, house.tv_a)
    await ring(hass)
    assert await tv.refusal(WRONG) == "wrong_pin"
    assert await tv.refusal(WRONG) == "wrong_pin"
    assert (await tv.let_in())["result"]["opened"] is True
    await ring(hass)
    assert await tv.refusal(WRONG) == "wrong_pin"
    assert await tv.refusal(WRONG) == "wrong_pin"
    assert await tv.refusal(WRONG) == "locked_out"


async def test_wrong_pins_count_across_tvs(hass, client, house):
    sub = next(s for s in house.entry.subentries.values() if s.data["tv_id"] == "tv_b")
    hass.config_entries.async_update_subentry(
        house.entry, sub, data={**sub.data, "let_in_allowed": True}
    )
    await hass.async_block_till_done()
    a, b = await link(client, house.tv_a), await link(client, house.tv_b)
    await ring(hass)
    assert await a.refusal(WRONG) == "wrong_pin"
    assert await b.refusal(WRONG) == "wrong_pin"
    assert await a.refusal(WRONG) == "locked_out"


async def test_lockout_survives_a_restart(hass, client, house):
    tv = await link(client, house.tv_a)
    await ring(hass)
    for _ in range(3):
        await tv.refusal(WRONG)
    await hass.async_block_till_done()
    await reload(hass, house.entry)
    tv = await link(client, house.tv_a)
    assert (await tv.manifest())["let_in_available"] is False
    await ring(hass)
    assert await tv.refusal() == "locked_out"
    # Two wrong tries, a restart, and one more still makes three.
    end_lock(hass)
    assert await tv.refusal(WRONG) == "wrong_pin"
    assert await tv.refusal(WRONG) == "wrong_pin"
    await reload(hass, house.entry)
    tv = await link(client, house.tv_a)
    await ring(hass)
    assert await tv.refusal(WRONG) == "locked_out"
    assert lock(hass).level == 2


# --- the seal --------------------------------------------------------------------


async def test_seals_that_do_not_open_are_refused_and_not_counted(hass, client, house):
    tv = await link(client, house.tv_a)
    other = await link(client, house.tv_a)
    await ring(hass)
    bad = [
        {"seal_cid": 99},  # sealed for another command id
        {"sealed": other.seal(PIN, 2)},  # sealed for another connection
        {"sealed": tv.seal(PIN, 3, credential=b"\x01" * 32)},  # wrong credential
        {"sealed": "A" * 102 + "=="},  # right size, not a seal
        {"sealed": "not base64!"},
    ]
    for kw in bad:
        data = await tv.let_in(**kw)
        assert data["error"]["code"] == "invalid_format", kw
    assert lock(hass).failures == 0
    assert house.door.opened == []
    assert (await tv.let_in())["result"]["opened"] is True


async def test_a_frame_cannot_be_replayed_on_its_connection(hass, client, house):
    tv = await link(client, house.tv_a)
    await ring(hass)
    sealed = tv.seal(WRONG, 1)
    await tv.ws.send_json({"id": 1, "type": "perch/let_in", "sealed_pin": sealed})
    assert (await recv(tv.ws))["result"]["refusal"] == "wrong_pin"
    await tv.ws.send_json({"id": 1, "type": "perch/let_in", "sealed_pin": sealed})
    assert (await recv(tv.ws))["error"]["code"] == "invalid_format"
    assert lock(hass).failures == 1


async def test_a_refused_frame_cannot_be_sent_again_after_a_ring(hass, client, house):
    tv = await link(client, house.tv_a)
    sealed = tv.seal(PIN, 1)
    await tv.ws.send_json({"id": 1, "type": "perch/let_in", "sealed_pin": sealed})
    assert (await recv(tv.ws))["result"]["refusal"] == "no_ring"
    await ring(hass)
    await tv.ws.send_json({"id": 1, "type": "perch/let_in", "sealed_pin": sealed})
    assert (await recv(tv.ws))["error"]["code"] == "invalid_format"
    assert house.door.opened == []


async def test_huge_command_id_is_refused_and_logged(hass, client, house):
    tv = await link(client, house.tv_a)
    await ring(hass)
    await tv.ws.send_json(
        {"id": 2**64, "type": "perch/let_in", "sealed_pin": "A" * 102 + "=="}
    )
    assert (await recv(tv.ws))["error"]["code"] == "invalid_format"
    assert house.logbook[-1]["message"] == "Lounge TV was refused: the request could not be read"
    tv.cid = 5
    assert (await tv.let_in())["result"]["opened"] is True


async def test_tv_name_links_are_not_in_the_logbook(hass, client, house):
    sub = next(s for s in house.entry.subentries.values() if s.data["tv_id"] == "tv_a")
    hass.config_entries.async_update_subentry(
        house.entry, sub, title="Den see https://evil.example/x www.evil.example"
    )
    await hass.async_block_till_done()
    tv = await link(client, house.tv_a)
    assert await tv.refusal() == "no_ring"
    assert house.logbook[-1]["message"] == "Den see was refused: no recent doorbell ring"


async def test_stale_options_form_does_not_clear_a_lockout(hass, client, house):
    flow = await hass.config_entries.options.async_init(house.entry.entry_id)
    lock(hass).level, lock(hass).disabled = 3, True  # locked out while the form is open
    await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {"advanced": {}, "doorbell": BELL,
         "let_in": {"let_in_enabled": True, "let_in_script": SCRIPT}},
    )
    assert lock(hass).disabled is True


async def test_two_requests_at_once_open_once(hass, client, house):
    a, b = await link(client, house.tv_a), await link(client, house.tv_a)
    await ring(hass)
    results = await asyncio.gather(a.let_in(), b.let_in())
    opened = sorted(r["result"]["opened"] for r in results)
    assert opened == [False, True]
    assert len(house.door.opened) == 1


async def test_missing_script_is_an_error_and_uses_the_ring(hass, client, house):
    tv = await link(client, house.tv_a)
    await ring(hass)
    hass.states.async_remove(SCRIPT)
    data = await tv.let_in()
    assert data["error"]["code"] == "unavailable"
    assert house.logbook[-1]["message"] == "Lounge TV could not run the let-in script"
    assert await tv.refusal() == "no_ring"
    assert house.door.opened == []


async def test_script_runs_with_its_own_context(hass, client, house):
    tv = await link(client, house.tv_a)
    await ring(hass)
    events = []
    hass.bus.async_listen("call_service", lambda e: events.append(e))
    assert (await tv.let_in())["result"]["opened"] is True
    await hass.async_block_till_done()
    call = next(e for e in events if e.data["domain"] == "script")
    assert call.data["service"] == "turn_on"
    assert call.data["service_data"] == {"entity_id": SCRIPT}
    assert call.context.user_id is None


# --- the PIN never leaks -----------------------------------------------------------


async def test_pin_is_never_logged_or_in_the_logbook(hass, client, house, caplog):
    caplog.set_level(logging.DEBUG)
    tv = await link(client, house.tv_a)
    await ring(hass)
    for _ in range(3):
        await tv.refusal(WRONG)
    end_lock(hass)
    await tv.let_in()
    await hass.async_block_till_done()
    text = caplog.text + repr(house.logbook) + repr(house.phone)
    for secret in (PIN, WRONG):
        assert secret not in text
    assert len(house.logbook) == 4
    assert all("Lounge TV" in e["message"] for e in house.logbook)


# --- options -----------------------------------------------------------------------


async def _options(hass, entry, let_in):
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        flow["flow_id"], {"advanced": {}, "doorbell": BELL, "let_in": let_in}
    )


@pytest.mark.parametrize(
    ("let_in", "error"),
    [
        ({"let_in_pin": "123456", "let_in_pin_confirm": "123457"}, "let_in_pin_mismatch"),
        ({"let_in_pin": "12345", "let_in_pin_confirm": "12345"}, "let_in_pin_format"),
        ({"let_in_pin": "12345x", "let_in_pin_confirm": "12345x"}, "let_in_pin_format"),
        ({"let_in_enabled": True, "let_in_pin": "123456", "let_in_pin_confirm": "123456"},
         "let_in_needs_script"),
        ({"let_in_enabled": True, "let_in_script": SCRIPT}, "let_in_needs_pin"),
    ],
)
async def test_options_refuse_a_bad_setup(hass, world, let_in, error):
    result = await _options(hass, world.entry, let_in)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


async def test_options_need_a_doorbell(hass, world):
    flow = await hass.config_entries.options.async_init(world.entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {"advanced": {}, "let_in": {
            "let_in_enabled": True, "let_in_script": SCRIPT,
            "let_in_pin": "123456", "let_in_pin_confirm": "123456"}},
    )
    assert result["errors"] == {"base": "let_in_needs_doorbell"}


async def test_options_store_only_a_hash_and_keep_it(hass, world):
    result = await _options(hass, world.entry, {
        "let_in_enabled": True, "let_in_script": SCRIPT,
        "let_in_pin": "90817263", "let_in_pin_confirm": "90817263"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    options = world.entry.options
    assert "90817263" not in repr(options)
    assert "let_in_pin" not in options and "let_in_pin_confirm" not in options
    stored = options["let_in_pin_hash"]
    assert crypto.verify_pin("90817263", stored)
    # Saving again without a PIN keeps the hash.
    await _options(hass, world.entry, {"let_in_enabled": True, "let_in_script": SCRIPT})
    assert world.entry.options["let_in_pin_hash"] == stored


async def test_tv_allow_list_in_reconfigure(hass, world):
    sid = next(
        k for k, s in world.entry.subentries.items() if s.data["tv_id"] == "tv_b"
    )
    flow = await world.entry.start_subentry_reconfigure_flow(hass, sid)
    room = world.entry.subentries[sid].data["room_id"]
    await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"room_id": room, "let_in_allowed": True}
    )
    assert world.entry.subentries[sid].data["let_in_allowed"] is True
