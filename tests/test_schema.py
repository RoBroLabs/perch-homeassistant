"""Every protocol fixture validates against the protocol schema."""

import json

import pytest

from .schema import PROTOCOL, validate

INDEX = json.loads((PROTOCOL / "fixtures" / "index.json").read_text())


@pytest.mark.parametrize("item", INDEX, ids=[i["file"] for i in INDEX])
def test_fixture_matches_schema(item):
    message = json.loads((PROTOCOL / "fixtures" / item["file"]).read_text())
    validate(item["def"], message)


def test_index_covers_every_fixture_file():
    on_disk = {
        str(p.relative_to(PROTOCOL / "fixtures"))
        for p in (PROTOCOL / "fixtures").rglob("*.json")
        if p.name != "index.json"
    }
    assert on_disk == {i["file"] for i in INDEX}
