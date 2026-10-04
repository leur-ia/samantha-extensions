---
name: spotify
description: Control the Spotify desktop app (what's playing, play/pause/next, play a track or artist) and search its catalog.
---
# Spotify

Tools:
- `spotify.now_playing {}` → `{running, state, title, artists, album, uri, position_s, length_s}`. "Qu'est-ce qui passe ?", "c'est quoi cette chanson ?".
- `spotify.control {action}`: `play`, `pause`, `toggle`, `next`, `previous`. "Pause", "suivant", "remets la musique".
- `spotify.open {uri}` plays a `spotify:` URI (track, album, playlist, artist, show, episode). Only URIs from `spotify.search` or `spotify.now_playing`: never make one up.
- `spotify.search {query, type?, limit?}` (`track` by default, `album`, `playlist`, `artist`; at most 10) → `{type, results: [{name, artists, uri, album?}]}`. Needs the user's own Spotify developer app; if it isn't set up, the error says how. Pass on that explanation and don't retry.

Answering:
- "Mets du Daft Punk": `spotify.search {query: "Daft Punk", type: "artist", limit: 1}` then `spotify.open` with its uri. A song title: search `track` and open the first result whose name and artist match what was asked. Say in one short sentence what is now playing.
- "Pause", "suivant", "plus fort" (volume isn't a Spotify tool: say so): `spotify.control`, then a one- or two-word confirmation.
- If `running` is false or a tool says Spotify isn't started, say so; offer to launch it (the Flatpak app `com.spotify.Client`) if a launch tool exists.
- Never invent a track, artist or uri. Answer in the user's language.
