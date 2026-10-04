---
name: home-assistant
description: The user's home through Home Assistant: lights, temperatures, blinds, climate, scenes; locks and alarm only with confirmation.
---
# Home Assistant

Tools (the server is `ha`: `home-assistant.…` in the registry):
- `entities {domain?, area?, search?}` → `{count, entities: [{entity_id, name, state, …}]}`. Find the entity before acting: "la lumière du salon" → `{domain: "light", area: "Salon"}` or `{search: "salon"}`.
- `state {entity_id}`: one entity. "Il fait combien dans la chambre ?".
- `control {entity_id, action, brightness?, temperature?, position?, volume?}`: on/off/toggle, open/close/stop for blinds, on for a scene, `set` with a value. Returns the new state.
- `call_service {domain, service, data}`: anything else: locks, alarm, garage doors, scripts. The user confirms each call; say what will happen first.

Answering:
- Act, then confirm in a few words from the returned state ("Le salon est allumé à 60 %.").
- Several entities match ("éteins tout en bas"): act on each `light` of the area, then say how many.
- Unlocking a door, disarming the alarm, opening the garage: always `call_service`, never guessed; if the request is ambiguous, ask.
- If a tool says Home Assistant isn't set up, pass on the instructions.
