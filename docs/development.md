# Perch for Home Assistant: developer notes

## Development

Needs Python 3.14 (Home Assistant 2026.9 needs 3.14.2 or later).

```
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements_test.txt
.venv/bin/pytest -q
```

CI runs the tests against pinned Home Assistant versions (2026.8, 2026.9 and 2026.10 today) and re-runs them monthly. New versions are added by hand when the test tools support them. `protocol/` is a pinned copy of the Perch app's protocol and is not edited here.

See [AGENTS.md](../AGENTS.md) for working rules.

## Moving the repository

The `OWNER` file is the single source of the GitHub owner name. After transferring the repository on GitHub, run `python3 scripts/set_owner.py NEWOWNER`, then `python3 scripts/check_owner.py` (CI runs it too), and commit the result. Do not edit owner names by hand.

## Licence

MIT. See [LICENSE](../LICENSE). Third-party material, including the word list used for the four-word check, is listed in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
