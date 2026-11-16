# Perch for Home Assistant

Perch is the Apple TV app by RoBroLabs. This integration lets Perch show the rooms, lights, scenes and cameras you choose, dim the lights while you watch, and show your doorbell camera when someone rings.

## What you need

- Home Assistant 2026.8 or later.
- The Perch app on an Apple TV, on the same home network as Home Assistant.
- Optional: the Frigate integration, for recent camera events.

## Install

1. In HACS, search for **Perch for Home Assistant** and choose **Download**.
2. Restart Home Assistant.

If the search finds nothing, add `https://github.com/RoBroLabs/perch-homeassistant` in HACS as a custom repository (type Integration).

## Pair a TV

You do this once per TV. You never type a token or a password.

1. In Home Assistant, go to **Settings > Devices & services > Add integration** and choose **Perch for Home Assistant**.
2. Open **Configure** on the Perch entry and choose what your TVs may use.
3. On the TV, open Perch and choose to pair with Home Assistant. The TV shows a 6-digit code.
4. In Home Assistant, on the Perch entry, choose **Pair a TV** and type the code.
5. Home Assistant and the TV each show four words. If they match, choose **The words match**. If not, choose **They do not match** and start again.
6. Set the TV's room: on the TV's row, open the three-dots menu and choose **Reconfigure**.

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
