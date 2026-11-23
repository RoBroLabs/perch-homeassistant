# Perch for Home Assistant: full guide

Perch for Home Assistant is the companion for Perch, the Apple TV app by RoBroLabs. It lets Perch show the rooms, lights, scenes and cameras you choose, dim the lights while you watch, and show your doorbell camera when someone rings. Each TV is paired once with a code shown on the screen, so no Home Assistant token is ever stored on the TV. It works on your home network by default.

## Requirements

- Home Assistant 2026.8.0 or later.
- The Perch app on an Apple TV, on the same local network as Home Assistant.
- HACS, for the easy install.

## Install

### With HACS

1. In HACS, search for **Perch for Home Assistant** and choose **Download**.
2. Restart Home Assistant.

If it is not listed, add it as a custom repository:

1. In HACS, open the three-dots menu and choose **Custom repositories**.
2. Paste `https://github.com/RoBroLabs/perch-homeassistant`, set the type to **Integration** and choose **Add**.
3. Find **Perch for Home Assistant** and choose **Download**.
4. Restart Home Assistant.

### By hand

1. Get the `custom_components/perch` folder from the repository (**Code > Download ZIP**) or from a release.
2. Copy it into the `custom_components` folder in your Home Assistant configuration folder. Create `custom_components` if it does not exist.
3. Restart Home Assistant.

## Add the integration and pair a TV

You do this once per TV. You never type a token or a password.

1. In Home Assistant, go to **Settings > Devices & services > Add integration** and choose **Perch for Home Assistant**. It asks you nothing.
2. Open **Configure** on the Perch entry and choose what your TVs may use (see [Options](#options)).
3. On the TV, open Perch and choose to pair with Home Assistant. The TV shows a 6-digit code.
4. In Home Assistant, on the Perch entry, choose **Pair a TV** and type the code.
5. Home Assistant shows four words and the TV shows four words. If they are the same, choose **The words match**. If not, choose **They do not match** and start again.
6. Set the TV's room: on the Perch entry, find the TV's row, open its three-dots menu and choose **Reconfigure** (see [Per-TV setup](#per-tv-setup)).

Start on the TV first. Home Assistant can only pair a TV that is waiting, and a pairing lasts 5 minutes. Pairing works only on your home network, never through Home Assistant Cloud.

To check everything end to end, follow [the first pairing check](first-pairing-check.md).

## Unpair a TV or remove Perch

- **One TV:** go to **Settings > Devices & services > Perch for Home Assistant**, open the TV's device and delete it. Its credential is revoked and its connection closes at once. To use that TV again, pair it afresh.
- **All of Perch:** stop playback on every TV first, because removing the entry mid-film does not restore the lights. Delete the integration entry, remove Perch in HACS (or delete `custom_components/perch` if you installed by hand), then restart Home Assistant.
- Perch's saved lighting state is in `.storage/perch.moods` in your configuration folder. Delete that file after removal for a clean slate. Otherwise a later reinstall may restore an old lighting snapshot after the restore timeout.

## What the TV can and cannot do

Each TV has a scope: the rooms you give it and the cameras and scenes you chose in Options. A TV can only use what is inside its scope. Everything else is refused.

**It can:**

- Show the rooms, lights, scenes and cameras in its scope, and switch lights on and off and set their brightness.
- Run the scenes you chose.
- Set the lights in its own room while it plays something (see [Moods](#moods)).
- Be told when your doorbell rings, with the doorbell camera if you chose one (see [Doorbell](#doorbell)).
- Open snapshots and live views of the cameras in its scope.
- Ask Perch to tell your phone when a request is ready to watch, if you switched that on (see [Tell my phone](#tell-my-phone)).
- Ask Perch to run your let-in script after a ring, if you set it up and allowed that TV (see [Let someone in](#let-someone-in)).

**It cannot:**

- Use Home Assistant as an administrator. Its credential works for Perch only and is refused by Home Assistant's own API.
- Use a light, scene or camera outside its scope. Switches are never included.
- See other devices, who is home, or any history.
- Open a door or touch a lock. Perch never uses a lock. The most it can do is ask to run the one let-in script you chose, and Home Assistant runs it only within 2 minutes of a ring, with the right let-in PIN, on a TV you allowed.
- Choose which phone gets a message, or what it says.
- Keep camera pictures. Perch does not store them.
- Send your house data anywhere. There is no Perch server, no cloud and no telemetry.

## Per-TV setup

Go to **Settings > Devices & services > Perch for Home Assistant**, find the TV's row, open its three-dots menu and choose **Reconfigure**.

- **This TV's room:** a Home Assistant area. The TV's lights and its moods work on this room.
- **Other rooms it may control:** optional extra areas. The TV's own room is always included.

The lights a TV sees are the light entities in those areas. A light counts as in an area if it is assigned to it, or has no area of its own and its device is. Disabled lights are left out. A TV with no room still pairs and can show cameras and scenes, but it controls no lights and moods do nothing for it.

## Profiles and Kids

The Perch app decides what each profile may do. The companion trusts what the TV tells it and does not enforce profile (Kids) rules for lights, scenes, playback moods, camera snapshots, live views or the doorbell. The one refusal it makes itself is camera events for a Kids profile, based on the profile the TV reports.

## Moods

When a TV plays something, Perch can set the lights in that TV's room.

| What the TV does | What happens |
|---|---|
| Film plays | Runs the Movie night scene if you chose one. Otherwise dims the room's lights to 10 %. |
| Episode plays | Dims the room's lights to 35 %. There is no scene for episodes. |
| Music plays | Nothing. |
| Paused | Runs the Paused scene if you chose one. Otherwise raises the lights Perch dimmed to 50 %, if they were brighter than that before. Nothing happens while another TV in the room is playing. |
| Stopped | Puts the lights back as they were. |

- **Dimming only goes down**, and never switches a light on. A light already at or below the mood level, or switched off, is left alone. Scenes do whatever the scene says.
- **Only after sunset.** Moods apply only if the sun is below the horizon when playback changes. If your Home Assistant has no `sun.sun` entity, Perch treats it as always after sunset.
- **A light you change by hand is left alone.** Perch stops managing it until the room is restored. A Movie night or Paused scene is held back while one of its lights has been changed by hand.
- **Two TVs in a room.** The lights come back only when no TV in the room is still playing or paused.
- **Restoring.** Perch saves the state of the lights before it first changes them and puts it back at the end. The snapshot is kept on disk, so a Home Assistant restart mid-film still restores. If a TV's connection drops, the room is restored after the restore timeout (15 minutes unless you changed it).
- **Skip tonight.** The app can switch lighting off until a time it chooses, at most 24 hours ahead. Moods then do nothing for that TV and its room is restored at once, unless another Perch TV there is still playing.
- **Lighting off.** If the app says lighting is off for the current profile, Perch puts the lights back.

Moods use the lights in the TV's own room. A scene may also move other lights. Perch restores the room's lights and any lights the scene lists. A scene from another integration that does not list its lights can only be restored for the room's own lights.

## Doorbell

Choose a doorbell trigger in Options. It can be:

- a `binary_sensor` (a ring is a change from off to on), or
- an `event` entity (a ring is any new event).

When it rings, every paired TV that is connected and has asked for doorbell alerts is told straight away. The alert carries the time and, if you chose one, the doorbell camera. If that camera is not one of your chosen cameras, no camera is sent. The doorbell is not limited to a room. A TV that is not connected at that moment misses the ring.

## Tell my phone

Perch can tell your phone when something you asked for is ready to watch. Off until you set it up.

1. Open **Configure** on the Perch entry and open **Tell my phone**.
2. Pick one or more phones (notify services, such as the Home Assistant app on your phone).
3. Switch on **Tell my phone when a request is ready**.

The message says "Your request is ready to watch". Switch on **Include the title's name** to say "<name> is ready to watch" instead; it is off by default because the name shows on a locked phone. Each title is told once, even if several TVs ask. A TV can ask at most 10 times an hour, and all TVs together 30 times an hour. The TV never chooses the phone or the words.

The same phones also get the let-in lockout notices.

## Let someone in

When the doorbell rings, an allowed TV can let someone in by running a script you choose. Off until you set it up. Perch never uses a lock itself: your script does whatever you write in it, for example unlock the door, wait 30 seconds and lock it again.

1. Write a script in Home Assistant that lets someone in.
2. Make sure a doorbell trigger is chosen in Options.
3. Open **Configure** on the Perch entry and open **Let someone in**. Pick the script, type a let-in PIN twice (6 to 12 digits; use one of its own, not a PIN you use anywhere else, and 8 or more digits if you can), and switch on **Let someone in from Perch**.
4. For each TV that may do it, open the TV, choose **Reconfigure** and switch on **This TV may let people in**. No TV may by default.

On the TV, after a ring, someone types the let-in PIN. Home Assistant checks, in this order: let-in is on, this TV is allowed, no lockout, a ring in the last 2 minutes that has not been used, and the PIN. Only then does it run your script. One ring lets someone in once. A restart forgets the ring.

**Wrong PINs.** Three wrong PINs in a row lock let-in for 15 minutes. Three more lock it for 1 hour. Three more switch it off until you switch it on again in Options. Each step tells your phones (see [Tell my phone](#tell-my-phone)) and shows a notification in Home Assistant. A lockout survives a restart. Switching let-in on again in Options clears every lockout.

**Logbook.** Every request, let in or refused, is a logbook entry naming the TV. The PIN is never written anywhere: Home Assistant keeps only a salted, slow hash of it.

The PIN the TV sends is sealed so that only this Home Assistant, on that connection, can read it, even over plain http.

Planned, not built yet: "Also ask on my phone", where a let-in would also need a yes on your phone.

## Camera events

A list of recent camera events is not part of this release. If the TV asks for one, Perch says events are unavailable. Nothing else breaks.

Live views and snapshots work for chosen cameras that offer a snapshot or a stream to Home Assistant.

## Options

Open **Configure** on the Perch entry. These apply to every TV.

| Option | What it does | Default |
|---|---|---|
| Cameras | The cameras your TVs may show. | None |
| Doorbell trigger | A `binary_sensor` or `event` entity that fires when someone rings. | None |
| Scenes | The scenes your TVs may run. | None |
| Put the lights back after (minutes) | How long Perch waits before putting the lights back when a TV disappears mid-film. From 5 to 60. | 15 |
| Doorbell camera | The camera shown when the doorbell rings. Chosen on the next page, from the cameras you picked. Offered only when you also chose a doorbell trigger. | None |
| Movie night scene | Run when a film starts. Chosen from the scenes you picked. | None |
| Paused scene | Run when playback is paused. Must differ from the Movie night scene. | None |
| Pairing attempts (under Advanced) | Wrong codes allowed before a pairing stops. From 3 to 10. | 5 |
| Allow Perch through Home Assistant Cloud (under Advanced) | Lets a TV reach Perch through Home Assistant Cloud when away from home. Pairing still needs your home network. | Off |
| Phones to tell (under Tell my phone) | The notify services Perch may use. | None |
| Tell my phone when a request is ready (under Tell my phone) | See [Tell my phone](#tell-my-phone). | Off |
| Include the title's name (under Tell my phone) | Puts the title in the message. Shows on a locked phone. | Off |
| Let someone in from Perch (under Let someone in) | See [Let someone in](#let-someone-in). Switching it on again clears a lockout. | Off |
| Script to run (under Let someone in) | The only thing let-in can do. | None |
| New let-in PIN (under Let someone in) | 6 to 12 digits, typed twice. Leave empty to keep the current one. | None |

Rooms and lights are set per TV, not here (see [Per-TV setup](#per-tv-setup)).

## Troubleshooting

**Home Assistant says no TV is waiting to pair.**
The TV has not started pairing, or its 5 minutes ran out. Open Perch on the TV, choose to pair with Home Assistant, then choose **Pair a TV** again.

**The code is rejected, or the pairing has expired.**
Start again on the TV so it shows a fresh code. After too many wrong codes (5 unless you changed it) the pairing stops.

**Pairing says two TVs answered.**
Two devices used the same code, so Perch stopped for safety. Someone else on your network may be pairing. Start again on the TV you want.

**The TV says `busy`.**
Another pairing is already open, or this TV or address already has a request in it. Wait for the open pairing to end (up to 5 minutes), or finish it, then try again.

**The TV says `not_local`.**
Home Assistant saw the request as coming from outside your home network, so it refused. Check that the TV and Home Assistant are on the same network, and that you are not using a VPN or guest network that separates them. If you use a reverse proxy, see [Security notes](#security-notes). A request through Home Assistant Cloud is refused for pairing with its own message, `pairing_local_only`.

**The TV says `rate_limited`.**
Too many requests came in, so Perch is pausing for a while. The reply says how many seconds to wait. Wait that long, then try again. The wait grows if it keeps happening, up to 15 minutes.

**The TV does not appear, or loses its connection.**
Both must be on the same network. If you are away from home, switch on **Allow Perch through Home Assistant Cloud** in Options (connections only, never pairing).

**It stopped working after Home Assistant restarted or updated.**
Restart Home Assistant once, then check **Settings > Devices & services > Perch for Home Assistant** for errors. Paired TVs stay paired across restarts. If it keeps failing, [open an issue](https://github.com/RoBroLabs/perch-homeassistant/issues) with your Home Assistant version and a debug log.

**Moods are not dimming.**
Check, in order: the TV has a room (Reconfigure); it is after sunset; lighting is on for that profile in the app; you did not use skip tonight; the room's lights are on, dimmable and above the mood level; and you did not change one by hand.

**The doorbell does not show.**
Check that a trigger is chosen in Options, that it is a `binary_sensor` or `event` entity, and that the TV is connected. For a camera picture, choose a doorbell camera from your chosen cameras.

**Camera events are unavailable.**
That is expected. This release has no camera events list.

**How do I get debug logs?**
Add this to `configuration.yaml`, then restart Home Assistant:

```yaml
logger:
  logs:
    custom_components.perch: debug
```

Or, without a restart, go to **Settings > Devices & services > Perch for Home Assistant** and choose **Enable debug logging** from the entry's menu. Read the log in **Settings > System > Logs**. Pairing codes and credentials are never logged.

## Security notes

- **Local network only.** Pairing and the TV's connection accept only local addresses. Remote access through Home Assistant Cloud is opt-in (see Options), and pairing never works that way.
- **Reverse proxy caveat.** If Home Assistant sits behind a reverse proxy, set `use_x_forwarded_for` and `trusted_proxies` correctly in the `http:` section of `configuration.yaml`. Otherwise every visitor can look like the proxy, which is local, so a remote client can pass as local. A proxy that is untrusted or set up wrongly defeats the local-network check. Make sure Perch's `/api/perch/` paths are not reachable from the internet unless you mean them to be.
- **One credential per TV.** Home Assistant keeps only a hash of it. The TV proves it holds the credential without sending it, and the credential is sealed to the TV's key when it is handed over. It works for Perch only, not on Home Assistant's own API.
- **Revoking a TV.** Delete the TV's device in Home Assistant. Its connection closes at once and it cannot reconnect.
- **Rate limiting.** Pairing starts, pairing status checks and TV connections are each limited per address and overall, per minute: pairing starts 10 per address and 30 overall, status checks 120 and 360, connections 10 and 60. Over a limit, the address (or everyone, for the overall limit) is paused for 60 seconds, doubling on each repeat up to 15 minutes. Global IPv6 addresses count per /64, so rotating addresses inside one network does not dodge the limit; IPv4, private, link-local and loopback addresses count one by one. Perch tracks at most 1,024 addresses at a time, forgetting the oldest first.
- **Perch cannot open a door by itself.** It never uses a lock. Let-in runs only the script you chose, only within 2 minutes of a ring Home Assistant saw itself, only with the let-in PIN, only on TVs you allowed, and locks after wrong PINs. A stolen TV credential gets no further than that: without the PIN it gets at most 9 guesses, each needing a fresh ring, before let-in switches off.
- **The let-in PIN is a boundary.** The Perch app also asks for its own Admin PIN, but Home Assistant does not trust anything the TV says about who is using it. Anyone who can read your Home Assistant backups can try to guess the PIN from its stored hash offline, so use a long PIN and keep backups private.
- **Lockout times use the clock.** Moving Home Assistant's clock forward shortens a timed lockout. The final switch-off does not depend on the clock.
- **Use https if you can.** Over plain http, camera pictures and links cross your home network unencrypted.
- **What is stored.** Home Assistant keeps, in the Perch entry, each TV's name, public key, room and scope, a hash of its credential, and your options. A salted scrypt hash of the let-in PIN is kept with your options. In its own storage it keeps the lights' saved state, any skip-tonight times, the let-in lockout state and hashes of the titles already told to your phone. Pairing attempts, short-lived camera links and the TV's pairing proof live in memory only and are gone on restart. The TV keeps its credential in its own keychain.
- **Reporting a security issue.** Please use GitHub private vulnerability reporting: open the repository's **Security** tab and choose **Report a vulnerability**, or go to `https://github.com/RoBroLabs/perch-homeassistant/security/advisories/new`. Do not post details in a public issue.

For developer notes, see [development.md](development.md).
