---
name: media
description: Music, radio and anything playing on the desktop: what's playing, pause, next, volume, find and play a song, an artist or a radio station.
---
# Media

One way to every player and catalog: MPRIS players (Spotify's app, browsers, YouTube Music, VLC…), Spotify's catalog, live radio, and whatever other providers are installed. Players and results carry their provider (`mpris:spotify`, `radio:radio`, `spotify:spotify:track:…`); take them from a call, never build one.

- `media.players {}` → `{players: [{player, name, state, title, artists, album}]}`. "Qu'est-ce qui passe ?": the one `playing`.
- `media.control {player, action}`: `play`, `pause`, `toggle`, `next`, `previous`, `stop`. For "pause" / "suivant", call `media.players` first and act on the one playing.
- `media.volume {player, level}`: a player's own volume (the system volume and output device are `audio.*`).
- `media.search {query, kind?, limit?}` → `{results: [{id, kind, name, …}], errors?}`: tracks, albums, artists, playlists, stations. `kind: "station"` for radio.
- `media.play {id}`: plays a search result.

Answering:
- "Mets du Daft Punk": `media.search {query: "Daft Punk", kind: "artist"}`, play the best match, say what plays in a few words.
- "Mets France Inter" / "une radio jazz": `media.search` with `kind: "station"`, play the first good match.
- A command gets a one- or two-word confirmation ("C'est en pause."). A question gets title and artist in one sentence.
- `errors` names a provider that failed (Spotify not set up, no network): mention it only if nothing else answered.
- Never invent a title, a station or an id.
