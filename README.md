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

Some are **fronts**: the one way Samantha talks about a domain (`mail`, `calendar`,
`media`…), served from every **provider** of it. A front declares its methods in
`capability.json`; a provider declares `[[provides]] capability = "media"` and
implements them. Adding a service to a domain is writing a provider.

| Extension | Kind | What Samantha gets |
|---|---|---|
| audio | | Speakers, headphones, volume, mute, Bluetooth audio |
| calendar | front | One agenda across calendar providers |
| contacts | front | One address book across providers |
| caldav | provider: calendar | CalDAV accounts and iCal feeds, meeting links, reminders |
| discord | | The user's own bot: servers, channels, messages, send |
| drives | | USB sticks and disks: mount, eject, plug notices |
| google | provider: mail, calendar, contacts | Gmail, Google Calendar (Meet links, reminders), Google Contacts |
| home-assistant | | Home Assistant states and controls |
| media | front | Everything playing, search and play across providers |
| microsoft | provider: mail, calendar | Outlook mail and calendar (Teams links) through Microsoft Graph |
| movies | front | Film ratings, details, trends, where to watch |
| mpris | provider: media | Any MPRIS player |
| notifications | | Notification history: what you missed |
| notion | | Notion search, pages, databases, writes |
| omdb | provider: movies | IMDb, Rotten Tomatoes, Metacritic ratings |
| phone | | KDE Connect: ring, battery, SMS, share |
| power | | Battery, power profile, brightness, suspend |
| radio | provider: media | Live radio (radio-browser.info) |
| slack | | The user's Slack through their own app token |
| spotify | provider: media | Spotify catalog search and play |
| system | | Load, processes, ports, errors, image updates |
| timers | | Timers and alarms on the island |
| tmdb | provider: movies | TMDB search, details, trends, where to watch (JustWatch) |
| vpn | | NetworkManager VPNs |

The mail front and its IMAP provider ship with Samantha itself.

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
