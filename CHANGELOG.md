# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

Proposed as 0.2.0. Needs Perch app support (WP-55 and WP-66) to be useful.

### Added
- Let someone in: after a ring, an allowed TV can run a script the admin chose, with a let-in PIN of its own (6 to 12 digits, kept only as a salted scrypt hash). Checks, in order: let-in on, TV allowed (none by default), no lockout, a ring in the last 2 minutes not used yet, the PIN. One ring lets someone in once; a restart forgets the ring. Three wrong PINs lock it for 15 minutes, then 1 hour, then switch it off until the admin switches it on again; lockouts survive a restart and each one tells the admin's phones. Every request is a logbook entry naming the TV. The TV seals the PIN to the connection, so it is safe over plain http.
- Tell my phone: the TV can ask Perch to tell the household's phones that a request is ready to watch. The admin picks the phones; the wording is fixed; the title's name is included only if the admin allows it. Once per title, 10 an hour per TV and 30 an hour in all.
- Protocol: `perch/let_in` is no longer reserved, with a new `disabled` refusal and the seal defined in `protocol/README.md` and `protocol/vectors/let_in.json`. New `perch/notify` command and `notify_available` in the manifest. A 0.1.x app, which never sends these, keeps working.

## [0.1.2] - 2026-11-17

### Removed
- Camera events from Frigate, and the picture and clip links that went with them. They relied on the Frigate integration's private data rather than anything it exposes publicly, so they could break on any Frigate update. The TV is now told events are unavailable. Live views, snapshots and the doorbell are unchanged.

## [0.1.1] - 2026-11-16

### Changed
- The HACS page is now a short guide for users, with the Perch logo, status badges and an Open in HACS button. The full guide moved to `docs/guide.md`.

## [0.1.0] - 2026-11-16

First early release.

### Added
- Pairing a TV with a 6-digit code shown on the screen, plus a four-word check. No Home Assistant token is stored on the TV. Pairing works only on the home network and is refused through Home Assistant Cloud.
- A private endpoint for the TV, with a credential per TV that Home Assistant stores only as a hash. The credential works for Perch only, and removing a TV's device revokes it at once.
- Options for cameras, a doorbell trigger and camera, scenes, the Movie night and Paused scenes, the restore timeout (5 to 60 minutes, default 15), pairing attempts (3 to 10, default 5) and an opt-in switch for Home Assistant Cloud.
- Per-TV scope: each TV has a room and optional extra rooms, and sees only the lights, scenes and cameras you allowed for it.
- Light and scene control from the TV, limited to that TV's scope.
- Moods: dimming for films (10 %) and episodes (35 %), a pause level (50 %) or scenes, only after sunset. Lights are restored at the end, a hand-changed light is left alone, two TVs in one room share the room, a restart mid-film still restores, and "skip tonight" is supported.
- Doorbell alerts from a `binary_sensor` or `event` entity, with an optional camera.
- Camera snapshots and live views for any Home Assistant camera, through short-lived links.
- Today's camera events from the official Frigate integration (version 5), with pictures and clips served through short-lived links. Frigate's address and credentials never reach the TV, and Kids profiles get no events.
- Brand icon, HACS metadata and a continuous-integration test run.

### Security
- Pairing and the TV's connection accept only local addresses by default. Pairing is always refused through Home Assistant Cloud.
- Rate limits on pairing requests, pairing status checks and TV connections, with a block that grows on repeated abuse.
- Rate limits check the overall limit before tracking a new address, count global IPv6 sources per /64 and track at most 1,024 addresses, so rotating addresses cannot grow memory without bound or slip past the per-address limit.
- Tests prove the local-only check behind a reverse proxy: with `use_x_forwarded_for` and `trusted_proxies` set, a public visitor is refused and a local one is allowed.

### Known limits
- New Frigate events are not pushed to the TV yet. The TV fetches them when asked.
- The companion does not enforce Perch profile (Kids) rules on lights, scenes, playback moods, camera snapshots, live views or the doorbell. The Perch app does. The one refusal the companion makes is camera events for a Kids profile, based on the profile the TV reports.
