"""`perch/notify` and the phone notify helper."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import ServiceCall
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from custom_components.perch.const import DOMAIN
from custom_components.perch.notify import clean_title_name
from custom_components.perch.runtime import get_runtime

from .test_endpoint import client, cmd, connect, world  # noqa: F401 - fixtures


class Phones:
    """Fake legacy notify services; `sent` lists (service, data)."""

    def __init__(self, hass, names=("mobile_app_robin", "mobile_app_sam")):
        self.sent: list[tuple[str, dict]] = []
        self.delay = 0.0
        self.fail: set[str] = set()
        for name in names:
            hass.services.async_register("notify", name, self._handler(name))

    def _handler(self, name):
        async def handle(call: ServiceCall) -> None:
            if self.delay:
                await asyncio.sleep(self.delay)
            if name in self.fail:
                raise RuntimeError("phone gone")
            self.sent.append((name, dict(call.data)))

        return handle


async def _set_up(hass, entry, **extra):
    options = {
        **entry.options,
        "notify_services": ["notify.mobile_app_robin"],
        "notify_request_ready": True,
        **extra,
    }
    hass.config_entries.async_update_entry(entry, options=options)
    await hass.async_block_till_done()


async def _notify(ws, cid, title_id="plex:1", **kw):
    return await cmd(
        ws, cid, "perch/notify", kind="request_ready", title_id=title_id, **kw
    )


async def test_not_set_up_is_refused(hass, client, world):
    Phones(hass)
    ws = await connect(client, world.tv_a)
    manifest = await cmd(ws, 1, "perch/manifest")
    assert manifest["result"]["notify_available"] is False
    data = await _notify(ws, 2)
    assert data["result"] == {"sent": False, "refusal": "not_set_up"}
    # Services picked but the switch off: still not set up.
    await _set_up(hass, world.entry, notify_request_ready=False)
    assert (await _notify(ws, 3))["result"]["refusal"] == "not_set_up"


async def test_sends_fixed_wording_without_the_name(hass, client, world):
    phones = Phones(hass)
    await _set_up(hass, world.entry)
    ws = await connect(client, world.tv_a)
    assert (await cmd(ws, 1, "perch/manifest"))["result"]["notify_available"] is True
    data = await _notify(ws, 2, title_name="The Wild Robot")
    assert data["result"] == {"sent": True, "refusal": None}
    assert phones.sent == [
        ("mobile_app_robin", {"title": "Perch", "message": "Your request is ready to watch"})
    ]


async def test_name_only_when_the_admin_allows_it_and_cleaned(hass, client, world):
    phones = Phones(hass)
    await _set_up(hass, world.entry, notify_include_title=True)
    ws = await connect(client, world.tv_a)
    await _notify(ws, 1, title_name="<b>Wild</b>\x07 [Robot]‮\n  2")
    assert phones.sent[0][1]["message"] == "bWild/b Robot 2 is ready to watch"


async def test_once_per_title_across_tvs_and_restarts(hass, client, world):
    phones = Phones(hass)
    await _set_up(hass, world.entry)
    ws = await connect(client, world.tv_a)
    assert (await _notify(ws, 1))["result"]["sent"] is True
    assert (await _notify(ws, 2))["result"] == {"sent": False, "refusal": "already_sent"}
    ws_b = await connect(client, world.tv_b)
    assert (await _notify(ws_b, 1))["result"]["refusal"] == "already_sent"
    assert (await _notify(ws_b, 2, title_id="plex:2"))["result"]["sent"] is True
    # A restart keeps what was sent.
    assert await hass.config_entries.async_reload(world.entry.entry_id)
    await hass.async_block_till_done()
    ws = await connect(client, world.tv_a)
    assert (await _notify(ws, 1))["result"]["refusal"] == "already_sent"
    assert len(phones.sent) == 2


async def test_every_picked_phone_is_told(hass, client, world):
    phones = Phones(hass)
    await _set_up(
        hass,
        world.entry,
        notify_services=["notify.mobile_app_robin", "notify.mobile_app_sam"],
    )
    phones.fail.add("mobile_app_sam")
    ws = await connect(client, world.tv_a)
    # One phone failing still counts as sent.
    assert (await _notify(ws, 1))["result"]["sent"] is True
    assert [s for s, _ in phones.sent] == ["mobile_app_robin"]


async def test_per_tv_and_overall_limits(hass, client, world):
    Phones(hass)
    await _set_up(hass, world.entry)
    ws = await connect(client, world.tv_a)
    for i in range(10):
        assert (await _notify(ws, i + 1, title_id=f"t{i}"))["result"]["sent"] is True
    data = await _notify(ws, 20, title_id="t10")
    assert data["result"] == {"sent": False, "refusal": "rate_limited"}
    # Another TV has its own count, under the overall one.
    get_runtime(hass).notifier._overall.limit = 11
    ws_b = await connect(client, world.tv_b)
    assert (await _notify(ws_b, 1, title_id="b1"))["result"]["sent"] is True
    assert (await _notify(ws_b, 2, title_id="b2"))["result"]["refusal"] == "rate_limited"


async def test_slow_phone_times_out_and_title_can_be_retried(hass, client, world):
    phones = Phones(hass)
    await _set_up(hass, world.entry)
    phones.delay = 1
    ws = await connect(client, world.tv_a)
    with patch("custom_components.perch.notify.NOTIFY_TIMEOUT", 0.05):
        data = await _notify(ws, 1)
    assert data["success"] is False and data["error"]["code"] == "unavailable"
    phones.delay = 0
    assert (await _notify(ws, 2))["result"]["sent"] is True


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "anything", "title_id": "plex:1"},
        {"kind": "request_ready"},
        {"kind": "request_ready", "title_id": ""},
        {"kind": "request_ready", "title_id": "x" * 129},
        {"kind": "request_ready", "title_id": "plex 1"},
        {"kind": "request_ready", "title_id": "plex:1", "title_name": ""},
        {"kind": "request_ready", "title_id": "plex:1", "title_name": "x" * 201},
        {"kind": "request_ready", "title_id": "plex:1", "title_name": "\x00\x07‮"},
        {"kind": "request_ready", "title_id": "plex:1", "title_name": 5},
    ],
)
async def test_bad_frames_are_invalid_format(hass, client, world, fields):
    phones = Phones(hass)
    await _set_up(hass, world.entry)
    ws = await connect(client, world.tv_a)
    data = await cmd(ws, 1, "perch/notify", **fields)
    assert data["error"]["code"] == "invalid_format"
    assert phones.sent == []


def test_clean_title_name_bounds():
    assert clean_title_name("a" * 150) == "a" * 100
    assert clean_title_name("a" * 200) == "a" * 100
    assert clean_title_name("a" * 201) is None
    assert clean_title_name("  Up\t\tHigh  ") == "Up High"
    assert clean_title_name("`*_#|~\\") is None


async def _entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_options_pick_registered_phones_only(hass):
    Phones(hass)
    hass.services.async_register("notify", "send_message", lambda call: None)
    entry = await _entry(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    phone_schema = flow["data_schema"].schema
    offered = next(
        v for k, v in phone_schema.items() if k == "phone"
    ).schema.schema["notify_services"].config["options"]
    assert [o["value"] for o in offered] == [
        "notify.mobile_app_robin",
        "notify.mobile_app_sam",
    ]
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            flow["flow_id"],
            {"advanced": {}, "phone": {"notify_services": ["notify.nope"]}},
        )
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {"advanced": {}, "phone": {"notify_request_ready": True}},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "notify_needs_service"}
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "advanced": {},
            "phone": {
                "notify_services": ["notify.mobile_app_sam"],
                "notify_request_ready": True,
            },
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["notify_services"] == ["notify.mobile_app_sam"]
    assert entry.options["notify_request_ready"] is True
    assert entry.options["notify_include_title"] is False
