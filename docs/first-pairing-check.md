# First pairing check

Use this page to check that Perch pairs with your Home Assistant and works end to end. It takes about 10 minutes and needs the Perch app on a TV on the same network.

Perch needs no Home Assistant token. Everything below is done in the Home Assistant interface and on the TV.

## Install

1. Open your Home Assistant configuration folder.
2. Copy the `custom_components/perch` folder of this repository into its `custom_components` folder (create that folder if it is missing). Copy only that folder, not `protocol/` or `tests/`.
3. Restart Home Assistant (Settings > System > Restart). Wait until `<your Home Assistant address>` answers again.

## Add the integration

1. Settings > Devices & services > Add integration > Perch. It asks nothing.
2. Open Perch > Configure and set what TVs may use:
   - Step 1: cameras, doorbell trigger, scenes and the restore timeout (5 to 60 minutes). Under Advanced: pairing attempts (3 to 10) and "Allow Perch through Home Assistant Cloud". Leave Cloud off unless you use Home Assistant Cloud.
   - Step 2: the doorbell camera, the Movie night scene and the Paused scene. All are optional, and the two scenes must differ.

## Pair a TV

1. On the TV, open Perch and choose to pair with Home Assistant.
2. On the Perch integration's page choose "Pair a TV" and type the 6-digit code shown on the TV.
3. Check that the four words are the same on both screens, then choose "The words match". Choose "They do not match" if they differ.
4. Pairing works only from your home network. Through Home Assistant Cloud it is always refused.

## What you should see

- A device for the TV on the Perch integration page, named as the TV is.
- Open the TV's entry and choose Reconfigure to set its room and the other rooms it may control. Until a room is set the TV has no room.
- The TV shows its room, lights, cameras, doorbell and scenes.
- A camera snapshot and a live stream open on the TV.

## Quick checks

- **Wrong code:** type a wrong code. Home Assistant says "That code is not right". After the allowed number of wrong codes the pairing stops.
- **Moods:** after sunset, with a room set for the TV and lighting turned on in the app, and with a Movie night scene set, start a film on the TV. The room dims. Pause it and the Paused scene runs. Stop it and, after the restore timeout at the latest, the lights return to how they were.
- **Doorbell:** trigger the doorbell sensor or event you chose (Developer tools > States works for a binary sensor). The TV shows the doorbell camera.
- **Frigate:** if the official Frigate integration is installed and its cameras are chosen, open the events view on the TV. Events appear and their pictures and clips play. Without Frigate the TV is told events are unavailable.
- **Logs:** open Settings > System > Logs and filter for "perch". There must be no code, credential or token in it.

## Remove a TV or Perch

- To unpair one TV, delete its device on the Perch integration page. The TV shows that it was unpaired and cannot reconnect.
- To remove Perch, open the integration page, choose the three dots next to the entry and Delete. Then delete the `custom_components/perch` folder and restart Home Assistant.
