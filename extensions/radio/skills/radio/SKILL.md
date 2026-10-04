---
name: radio
description: Live radio from around the world: find and play a station by name, country or genre; stop it.
---
# Radio

Tools:
- `radio.search {query?, country?, tag?, limit?}` → `{stations: [{id, name, country, tags, codec, bitrate}]}`, most listened first. `country` is a code (FR, BE, CH, CA, US…), `tag` a genre (jazz, news, classical, rock, lofi…).
- `radio.play {id?, query?}`: plays a station (an id from search, or a name: the most listened match). Replaces the current one.
- `radio.stop {}`, `radio.now {}`.

Answering:
- "Mets France Inter": `radio.play {query: "France Inter"}`, then "C'est parti, France Inter."
- "Mets du jazz" / "une radio belge": `radio.search` with `tag` or `country`, play the first result, name it.
- Several results look alike and the user named a precise station: prefer the exact name match from `radio.search`.
- Volume is the `audio` extension's. Never invent a station.
