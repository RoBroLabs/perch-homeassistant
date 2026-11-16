# Perch for Home Assistant: contributor and agent note

## Style
- Perch branding only.
- British English, plain and short, no em dashes.
- Do not edit `protocol/` here. It is a pinned copy of the Perch app's protocol; update it
  only by copying a new version across.

## Gates
- Contributors and coding agents work on a branch and open a pull request. Maintainers merge.
- Never print, log, commit or store a token or secret.
- Releases, HACS listing and repository settings are for maintainers only.

## Testing
- Python 3.14 (Home Assistant 2026.9 needs 3.14.2 or later).
- Set up: `uv venv --python 3.14 .venv && uv pip install --python .venv/bin/python -r requirements_test.txt`
- Run: `.venv/bin/pytest -q`
- CI (`.github/workflows/tests.yml`) runs pytest against three explicit Home Assistant and
  pytest-homeassistant-custom-component pairs; update them by hand.
- `validate.yml` runs hassfest and the HACS action. The HACS action fails on a private fork.
