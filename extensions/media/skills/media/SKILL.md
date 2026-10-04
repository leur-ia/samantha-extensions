---
name: media
description: Control any media player (Spotify, browser, YouTube Music, VLC…): what's playing, play, pause, next, previous, player volume.
---
# Media

Tools (`player` is a name fragment like `spotify`, `firefox`; omit it to act on what is playing):
- `media.now_playing {player?}` → `{player, name, state, title, artists, album, length_s}`. "C'est quoi cette musique ?".
- `media.players {}`: every open player and what it plays.
- `media.control {action, player?}`: `play`, `pause`, `toggle`, `next`, `previous`, `stop`. "Pause", "suivant", "relance la vidéo".
- `media.volume {level, player?}`: the player's own volume 0–100 (system volume and output devices are the `audio` extension's).

Answering:
- A command gets a one- or two-word confirmation ("C'est en pause."). A question gets title and artist in one sentence.
- Several players and an ambiguous request ("mets pause" while two play): act on the one playing; if both play, ask which.
- To play something new on Spotify, use the `spotify` extension's search if it is installed.
- Never invent a title. If no player is open, say so.
