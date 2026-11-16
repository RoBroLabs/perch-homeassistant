"""Validate messages against the protocol schema."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

PROTOCOL = Path(__file__).parent.parent / "protocol"
SCHEMA = json.loads((PROTOCOL / "schema" / "perch.schema.json").read_text())


def validate(def_name: str, message) -> None:
    """Raise jsonschema.ValidationError unless `message` fits `$defs/def_name`."""
    Draft202012Validator({**SCHEMA, "$ref": f"#/$defs/{def_name}"}).validate(message)
