# Samantha extensions

Extensions for [Samantha](https://gitlab.com/jeanbaptiste2/samantha), the intelligent
shell for a Linux desktop, and the registry Samantha installs them from.

```
samantha extension search            # what's here
samantha extension install spotify   # review, then install the pinned commit
samantha extension update            # newer releases, reviewed again
```

Each extension is a directory under `extensions/`: a manifest (`extension.toml`), a
stdio MCP server in Python 3 standard library only, its tests (no network), and a skill.
They use official access only: public APIs with the user's own tokens, D-Bus, the
system's own tools. Secrets live in the Secret Service, never in files.

| Extension | What Samantha gets |
|---|---|
| audio | Speakers, headphones, volume, mute, Bluetooth audio |
| calendar | CalDAV and iCal agenda, new events, reminders |
| discord | The user's own bot: servers, channels, messages, send |
| drives | USB sticks and disks: mount, eject, plug notices |
| home-assistant | Home Assistant states and controls |
| media | Any MPRIS player |
| notifications | Notification history: what you missed |
| notion | Notion search, pages, databases, writes |
| phone | KDE Connect: ring, battery, SMS, share |
| power | Battery, power profile, brightness, suspend |
| radio | Live radio (radio-browser.info) |
| slack | The user's Slack through their own app token |
| spotify | Spotify desktop control and catalog search |
| system | Load, processes, ports, errors, image updates |
| timers | Timers and alarms on the island |
| vpn | NetworkManager VPNs |

## The registry

`index/<id>.toml` lists an extension's releases, each pinned to a commit:

```toml
description = "One line, what the extension does."

[[release]]
version = "0.1.0"
source = "https://github.com/leur-ia/samantha-extensions.git"   # any git URL
path = "extensions/spotify"                                      # its directory there
commit = "<40-hex commit>"                                       # the reviewed commit
```

Installing fetches exactly `commit` from `source`, checks it is what came back, shows
the review (sandbox, tools, hooks) and asks. A release is never edited: a new one is
added. Extensions from other repositories are welcome: open a pull request adding
their index file. Other registries: `samantha registry add <name> <url>[#path]`.

## Tests

```
for d in extensions/*/; do (cd "$d" && python3 -m unittest -q); done
```
