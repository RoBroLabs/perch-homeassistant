<img src="https://raw.githubusercontent.com/RoBroLabs/perch-homeassistant/main/custom_components/perch/brand/logo@2x.png" alt="Perch" height="120">

[![release](https://img.shields.io/github/v/release/RoBroLabs/perch-homeassistant?label=release)](https://github.com/RoBroLabs/perch-homeassistant/releases)
[![build](https://img.shields.io/github/actions/workflow/status/RoBroLabs/perch-homeassistant/tests.yml?branch=main&label=build)](https://github.com/RoBroLabs/perch-homeassistant/actions/workflows/tests.yml)
[![validate](https://img.shields.io/github/actions/workflow/status/RoBroLabs/perch-homeassistant/validate.yml?branch=main&label=validate)](https://github.com/RoBroLabs/perch-homeassistant/actions/workflows/validate.yml)
[![license](https://img.shields.io/github/license/RoBroLabs/perch-homeassistant?label=license)](https://github.com/RoBroLabs/perch-homeassistant/blob/main/LICENSE)
[![HACS](https://img.shields.io/badge/HACS-custom-orange)](https://hacs.xyz)

# Perch for Home Assistant

Perch is the Apple TV app by RoBroLabs. This integration lets Perch show the rooms, lights, scenes and cameras you choose, dim the lights while you watch, and show your doorbell camera when someone rings.

## What you need

- Home Assistant 2026.8 or later.
- The Perch app on an Apple TV, on the same home network as Home Assistant.
- Optional: the Frigate integration, for recent camera events.

## Install

Easiest install is through HACS:

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=RoBroLabs&repository=perch-homeassistant&category=integration)

1. Choose **Download**, then restart Home Assistant.
2. Go to **Settings > Devices & services > Add integration** and choose **Perch for Home Assistant**.

## Pair a TV

You do this once per TV. You never type a token or a password.

1. Open **Configure** on the Perch entry and choose what your TVs may use.
2. On the TV, open Perch and choose to pair with Home Assistant. The TV shows a 6-digit code.
3. In Home Assistant, on the Perch entry, choose **Pair a TV** and type the code.
4. Home Assistant and the TV each show four words. If they match, choose **The words match**. If not, choose **They do not match** and start again.
5. Set the TV's room: on the TV's row, open the three-dots menu and choose **Reconfigure**.

Start on the TV first. A pairing lasts 5 minutes and works only on your home network.

## What Perch can do

- Show the rooms and lights you give each TV, and switch them on and off or dim them.
- Run the scenes you choose.
- Dim the lights in the TV's room while a film plays, and put them back afterwards.
- Show your doorbell camera on the TV when someone rings.
- Show recent camera events from Frigate.
- Give each TV its own scope, so one TV can see less than another.
- Keep camera events away from Kids profiles.

What Perch never does:

- Use anything outside a TV's scope.
- Store a Home Assistant token on the TV.
- Pair from outside your home network.

## Need more?

- [Full guide](https://github.com/RoBroLabs/perch-homeassistant/blob/main/docs/guide.md): options, moods, doorbell, Frigate, troubleshooting and security.
- [Developer notes](https://github.com/RoBroLabs/perch-homeassistant/blob/main/docs/development.md)
- [Report a problem](https://github.com/RoBroLabs/perch-homeassistant/issues)

MIT licence.
