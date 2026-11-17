"""`perch/events` in a release with no camera events source: the checks on the
request still run, then the TV is told events are unavailable."""

from __future__ import annotations

from .test_endpoint import client, cmd, connect, world  # noqa: F401  (fixtures)

DAY = "2026-11-15"


async def _events(client, world, **fields):  # noqa: F811
    ws = await connect(client, world.tv_a)
    fields = {"day": DAY, "role": "adult"} | fields
    data = await cmd(ws, 5, "perch/events", **fields)
    await ws.close()
    return data


async def test_events_unavailable(client, world):  # noqa: F811
    for role in ("adult", "teen"):
        data = await _events(client, world, role=role)
        assert data["error"]["code"] == "unavailable"
    data = await _events(client, world, camera_id=world.camera)
    assert data["error"]["code"] == "unavailable"


async def test_kid_refused(client, world):  # noqa: F811
    data = await _events(client, world, role="kid")
    assert data["error"]["code"] == "refused_for_role"


async def test_other_camera_not_allowed(client, world):  # noqa: F811
    data = await _events(client, world, camera_id=world.camera_other)
    assert data["error"]["code"] == "not_allowed"


async def test_bad_request_is_invalid_format(client, world):  # noqa: F811
    for fields in ({"day": "yesterday"}, {"day": "2026-13-40"}, {"role": "guest"}):
        data = await _events(client, world, **fields)
        assert data["error"]["code"] == "invalid_format"


async def test_media_path_is_gone(client, world):  # noqa: F811
    resp = await client.get("/api/perch/media/anything/snapshot")
    assert resp.status == 404


def test_nothing_reads_another_integration(hass):
    """No module names the Frigate integration, let alone its private data."""
    from pathlib import Path

    import custom_components.perch as perch

    for path in Path(perch.__file__).parent.glob("*.py"):
        assert "frigate" not in path.read_text().lower(), path.name
