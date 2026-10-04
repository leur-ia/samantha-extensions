---
name: audio
description: Sound devices and volume: switch speakers/headphones, volume up/down, mute, microphone, connect Bluetooth headphones.
---
# Audio

Tools (`kind` is `output`, the default, or `input` for microphones):
- `audio.devices {}` → `{devices: [{id, kind, name, default, volume, muted, bluetooth}]}`.
- `audio.use {device, kind?}`: make it the default ("mets le son sur le casque", "utilise le micro USB").
- `audio.volume {level?, change?, mute?, device?, kind?}`: `level` 0–100, `change` +10/−10 ("plus fort" = +10, "beaucoup plus fort" = +25), `mute` true/false/toggle. No arguments: read it.
- `audio.bluetooth {}`: paired devices, connected or not.
- `audio.bluetooth_connect {device, connect?}`: connect (or `connect: false` disconnect) a paired device.

Answering:
- "Mets le son sur mon casque": if the headphones are Bluetooth and not listed among outputs, `audio.bluetooth_connect` first, then `audio.use`. Confirm in a few words.
- Volume changes: say the new level ("Volume à 35 %").
- Pairing a new device isn't possible here: tell the user to pair it once from the system settings.
- A player's own volume (Spotify's slider) is the `media` extension's.
