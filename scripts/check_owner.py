#!/usr/bin/env python3
"""Fail if any file names a GitHub owner other than the one in OWNER."""

import sys

from _owner import ROOT, patterns_for, read_owner, text_files


def main() -> int:
    owner = read_owner()
    bad = []
    for path, text in text_files():
        for pattern in patterns_for(path):
            for match in pattern.finditer(text):
                if match.group("o") != owner:
                    bad.append(f"{path.relative_to(ROOT)}: {match.group(0)}")
    if bad:
        print(f"Owner mismatch (OWNER is {owner}):")
        print("\n".join(bad))
        return 1
    print(f"Owner check passed ({owner}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
