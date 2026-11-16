"""Shared helpers for set_owner.py and check_owner.py.

OWNER (repository root) is the single source of the GitHub owner name.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OWNER_FILE = ROOT / "OWNER"
REPO = "perch-homeassistant"
NAME = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
SKIP_DIRS = {".git", ".venv", "spike", ".claude", "__pycache__", "node_modules"}

# Each pattern captures the owner in the group "o".
PATTERNS = [
    re.compile(rf"github\.com/(?P<o>{NAME})/{REPO}"),
    re.compile(rf"githubusercontent\.com/(?P<o>{NAME})/{REPO}"),
    re.compile(rf"owner=(?P<o>{NAME})&(?:amp;)?repository={REPO}"),
]
# Code owner entry, only in manifest.json files: a user ("@owner") or an
# organisation team ("@owner/team"); only the owner part is checked or swapped.
CODEOWNER = re.compile(r'"@(?P<o>[A-Za-z0-9-]+)(?:/[A-Za-z0-9_.-]+)?"')


def read_owner() -> str:
    return OWNER_FILE.read_text().strip()


def text_files():
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or SKIP_DIRS & set(path.relative_to(ROOT).parts):
            continue
        try:
            yield path, path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue


def patterns_for(path: Path):
    if path.name == "manifest.json":
        return PATTERNS + [CODEOWNER]
    return PATTERNS
