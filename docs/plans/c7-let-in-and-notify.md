# Plan: C-7 let-in, and notify

Unblocks two Perch work packages: WP-55 (let someone in from the doorbell overlay) and
WP-66 (tell my phone when a request is ready, app issue #122). Spec: the Perch app's
`docs/HOUSE-COMPANION.md` section 9, threat rows in 5.2 and milestone C-7.

## Decisions (made by the project lead for an unattended run; the owner may change any)

1. Let-in runs a Home Assistant script the admin picks. The companion never calls a lock.
2. The let-in PIN is its own PIN (not Plex, not Perch), 6 to 12 digits, set in options, kept
   only as a salted scrypt hash, never logged, checked in constant time. The companion's
   check is the boundary; any role claim from the TV is ignored.
3. Ring window 2 minutes. A successful let-in uses up the ring. A restart forgets the ring.
4. Lockout: 3 wrong tries lock for 15 minutes, the next 3 for 1 hour, the next 3 turn
   let-in off until the admin turns it on again in options. Kept across restarts.
5. Per-TV allow-list, no TV allowed by default. `let_in_available` is true for a TV only
   with a script, a PIN, the feature on, that TV allowed and no lockout.
6. "Also ask on my phone" is not built. Documented as planned.
7. Notify: new command `perch/notify`, fixed kinds (only `request_ready`), wording owned by
   the companion, admin-picked `notify.*` services, `notify_available` in the manifest,
   once per title, per-TV and overall limits, bounded strings, off until set up. The same
   helper sends the admin the let-in lockout notices.
8. Every let-in request, allowed or refused, is a logbook entry naming the TV, never the PIN.
9. `sealed_pin`: ephemeral X25519 to the companion's key, HKDF-SHA256, ChaCha20-Poly1305,
   with test vectors. See "Sealing" below for where this departs from the brief.
10. Protocol: drop the `x-reserved` markers for what is built, add a `disabled` refusal and
    the notify frames, stay compatible with a 0.1.2-era app, propose 0.2.0 (not tagged).

## Sealing

The companion has no long-lived key today. Each connection has a fresh `server_key`
(sent in `auth_required`). A new static key would make a sealed PIN replayable on any
later connection by anyone holding a stolen TV credential, so the PIN is sealed to the
connection's `server_key` instead, with X25519(TV static key, `server_key`) and the stored
credential hash mixed in (so a host pretending to be the companion cannot open it, and a
leaked hash alone cannot seal) and the command id
in the AAD (so a frame cannot be replayed on the same connection). Fixed 16-byte padded
plaintext so the length of the PIN does not show.

## Tasks and files

| Step | Files |
|---|---|
| Notify helper and options | `notify.py` (new), `const.py`, `config_flow.py`, `strings.json`, `translations/en.json` |
| `perch/notify` | `commands.py`, `scope.py`, schema, fixtures, `protocol/README.md` |
| Let-in | `let_in.py` (new), `crypto.py`, `endpoint.py`, `runtime.py`, `__init__.py`, `hub.py`, `commands.py`, `scope.py`, `config_flow.py` (options section, TV reconfigure), schema, fixtures, vectors |
| Docs | `docs/guide.md`, `README.md`, `SECURITY.md`, `CHANGELOG.md`, `manifest.json`, `docs/notes/app-side-handover.md` |

Storage: let-in lockout in a `Store` (`perch.let_in`), saved at once on every change.
Notify once-per-title keys in a `Store` (`perch.notify`), bounded.

## Tests

New `tests/test_let_in.py`, `tests/test_notify.py`, `tests/test_let_in_crypto.py`; plus the
suites of touched files: `test_options.py`, `test_endpoint.py`, `test_schema.py`,
`test_scope.py`, `test_crypto.py`, `test_subscribe.py`.

Let-in: each refusal (no ring, old ring, used ring, wrong PIN, locked out at each step, not
allowed, disabled), success, lockout kept over a restart, ring forgotten after a restart,
role claim ignored, no PIN in any log or logbook entry, sealing vectors, schema parity.
Notify: success, not set up, rate limits, once per title, string bounds, slow service.

## Out of scope

Phone confirm (decision 6). Any real Home Assistant or real lock. Tagging, release, HACS.

## Note on `protocol/`

`AGENTS.md` says `protocol/` is a pinned copy of the app's protocol. Tonight the protocol
change is made here first, on the lead's instruction, and `docs/notes/app-side-handover.md`
lists what the app repository must copy so both sides match again.
