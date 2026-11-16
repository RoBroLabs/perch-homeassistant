#!/usr/bin/env python3
"""Rewrite the GitHub owner everywhere: python3 scripts/set_owner.py NEWOWNER."""

import re
import sys

from _owner import NAME, OWNER_FILE, ROOT, patterns_for, text_files


def swap(match: re.Match, new: str) -> str:
    start = match.start()
    text = match.group(0)
    return text[: match.start("o") - start] + new + text[match.end("o") - start :]


def main() -> int:
    if len(sys.argv) != 2 or not re.fullmatch(NAME, sys.argv[1]):
        print("Usage: python3 scripts/set_owner.py NEWOWNER")
        return 2
    new = sys.argv[1]
    changed = 0
    for path, text in text_files():
        updated = text
        for pattern in patterns_for(path):
            updated = pattern.sub(lambda m: swap(m, new), updated)
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            changed += 1
            print(f"updated {path.relative_to(ROOT)}")
    OWNER_FILE.write_text(new + "\n")
    print(f"Owner set to {new} ({changed} files changed). Run scripts/check_owner.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
