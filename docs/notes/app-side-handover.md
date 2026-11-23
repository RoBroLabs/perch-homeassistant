# Handover: what the Perch app must mirror for companion 0.2.0

For WP-55 (let someone in from the doorbell overlay) and WP-66 (tell my phone when a request
is ready, #122). The protocol change was made in this repository first; the app copies it.
The protocol number stays `1`: everything is additive, and a 0.1.x companion answers
`perch/let_in` with `not_supported_yet` and `perch/notify` with `unknown_command`.
`auth_ok.companion_version` is `0.2.0` from this release.

## 1. Files to copy unchanged into the app's `protocol/`

Copy the whole `protocol/` folder. Changed or new:

- `README.md` (let-in checks and seal, `perch/notify`)
- `schema/perch.schema.json`
- `vectors/let_in.json` (new)
- `fixtures/index.json`
- `fixtures/commands/let_in.request.json` (real vector value now), `let_in.result.json`
  (now the success case), `let_in.result.{disabled,not_allowed,locked_out,no_ring,wrong_pin}.json`
- `fixtures/commands/notify.request.json`, `notify.request.no_name.json`,
  `notify.result.json`, `notify.result.{already_sent,not_set_up,rate_limited}.json`
- `fixtures/commands/error.invalid_format.json`
- `fixtures/commands/manifest.result.json`, `manifest.result.no_room.json` (gain `notify_available`)

Schema changes: `x-reserved` removed from `LetInCommandFrame`, `LetInResult`,
`LetInResultFrame`. `sealed_pin` is now exactly 76 bytes (pattern `^[A-Za-z0-9+/]{102}==$`).
`LetInResult.refusal` gains `disabled`. New `NotifyKind`, `NotifyCommandFrame`,
`NotifyResult`, `NotifyResultFrame`, added to `CommandFrame` and `ResultFrame`.
`ManifestResult` gains optional `notify_available` (missing means false).

## 2. The let-in seal, step by step

Inputs on the TV: the current connection's `server_key` and `challenge` (from
`auth_required`), the TV's static X25519 private key and public key, its 32-byte
credential, the PIN, and the `id` of the let-in command frame being sent.

1. Check the PIN is 6 to 12 ASCII digits.
2. `eph` = a fresh `Curve25519.KeyAgreement.PrivateKey()`.
3. `shared = eph.sharedSecretFromKeyAgreement(with: serverKey)` (32 raw bytes).
4. `staticShared = tvPriv.sharedSecretFromKeyAgreement(with: serverKey)` (the same value the
   auth proof uses).
5. `ikm = shared || staticShared || SHA256(credential)` (96 bytes).
6. `key = HKDF<SHA256>.deriveKey(inputKeyMaterial: ikm, salt: challenge,
   info: "perch-letin-v1" || ephPub || serverKey || tvPub, outputByteCount: 32)`.
7. `aad = "perch-letin-v1" || challenge || UInt64(id).bigEndian bytes`.
8. `plaintext` = PIN ASCII bytes padded with `0x00` to 16 bytes.
9. `ChaChaPoly.seal(plaintext, using: key, nonce: fresh 12 bytes, authenticating: aad)`.
10. Wire: `ephPub(32) || nonce(12) || ciphertext(16) || tag(16)`, base64 with padding (104
    characters).

Rules: seal a fresh PIN for every request; never reuse a seal or an id; a seal made on one
connection does not open on another (reconnect means re-seal). Never store or log the PIN
or the seal. Use a new, strictly increasing `id` for each let-in on a connection: the
companion refuses an id it already saw for let-in, even after a refusal.

Vectors (`protocol/vectors/let_in.json`): inputs `tv_private_key`, `server_ephemeral_private_key`,
`challenge` and `credential` are the same as in `vectors/crypto.json`;
`let_in_ephemeral_private_key = 6162...7f80`, `let_in_aead_nonce = 303132333435363738393a3b`,
`pin = "13572468"`, `command_id = 11`. Outputs list every step (`server_key`, `shared`,
`static_shared`, `credential_hash`, `ikm`, `info`, `key`, `aad`, `plaintext`, `sealed_pin`,
`sealed_pin_base64`). Opening with command id 12, a wrong stored hash or another TV public
key must fail. The vectors were made with Python `cryptography`, not CryptoKit: the app's
test must reproduce them byte for byte, and if it cannot, stop and raise it here.

## 3. Manifest fields

- `let_in_available` (bool, was always false): true only when let-in is set up and on,
  this TV is allowed, and no lockout runs. Show the let-in action only when true. The
  companion pushes `manifest` when it changes (lockout starts or ends, options, allow-list).
- `notify_available` (bool, optional): true when a request notice would be sent. Missing
  means false.

## 4. Let-in results and suggested wording

`{opened: true, refusal: null}` means Home Assistant started the household's let-in script.

| Result | Suggested plain wording |
|---|---|
| opened | "Door opened" (or "Letting them in") |
| `wrong_pin` | "That PIN is not right." |
| `locked_out` | "Too many wrong PINs. Try again later." |
| `no_ring` | "This only works just after the doorbell rings." |
| `not_allowed` | "This TV is not set up to let people in." |
| `disabled` | "Letting people in is switched off in Home Assistant." |
| error `invalid_format` | "Something went wrong. Try again." (re-seal on a new id) |
| error `unavailable` | "The door could not be opened." |

The companion ignores any role. Keep the app's own parent and fresh Admin PIN checks
(PERMISSIONS.md) as the convenience layer; the companion's PIN is the boundary. The let-in
PIN is a separate PIN the admin set in Home Assistant, not the Admin PIN or a Plex PIN.

## 5. `perch/notify`

Request `{id, type: "perch/notify", kind: "request_ready", title_id, title_name?}`.
`title_id`: 1 to 128 of `A-Z a-z 0-9 . _ : / -`, stable per title (for example `plex:<ratingKey>`
or `tmdb:movie:<id>`); used only to tell each title once. `title_name`: optional, 1 to 200
characters; the companion includes it only if the admin allows names.

Result `{sent, refusal}`: `sent: true`; or `not_set_up`, `already_sent`, `rate_limited`
(10 an hour per TV, 30 in all). Error `unavailable` means no phone took it; the app may try
the same title later. The app sends it only when `notify_available` is true, from the
household automation the parent switched on (DECISIONS.md, "Phone notice"), and never
names a service. User-facing wording says "your phone".

## 6. Not built

"Also ask on my phone" for let-in is planned, not built.
