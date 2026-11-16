# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
