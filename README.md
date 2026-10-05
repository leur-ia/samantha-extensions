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
| clipboard | | Clipboard history (passwords never kept) |
| code | front | GitHub/GitLab inbox, reviews, PRs/MRs, CI |
| contacts | front | One address book across providers |
| backup | | restic backups: back up, snapshots, restore |
| caldav | provider: calendar | CalDAV accounts and iCal feeds, meeting links, reminders |
| discord | | The user's own bot: servers, channels, messages, send |
| duffel | provider: flights | Flight offers from 300+ airlines (search only) |
| drives | | USB sticks and disks: mount, eject, plug notices |
| files | front | Cloud storage: browse, search, download, upload |
| flights | front | Find and compare flights, a link to book |
| github | provider: code | GitHub notifications, reviews, Actions |
| gitlab | provider: code | GitLab to-dos, reviews, pipelines |
| google | provider: mail, calendar, contacts | Gmail, Google Calendar (Meet links, reminders), Google Contacts |
| google-places | provider: restaurants | Google ratings, hours, reviews, reservations |
| home-assistant | | Home Assistant states and controls |
| media | front | Everything playing, search and play across providers |
| microsoft | provider: mail, calendar | Outlook mail and calendar (Teams links) through Microsoft Graph |
| movies | front | Film ratings, details, trends, where to watch |
| mpris | provider: media | Any MPRIS player |
| notifications | | Notification history: what you missed |
| notion | | Notion search, pages, databases, writes |
| omdb | provider: movies | IMDb, Rotten Tomatoes, Metacritic ratings |
| osm | provider: restaurants | OpenStreetMap restaurants, free (no ratings) |
| phone | | KDE Connect: ring, battery, SMS, share |
| power | | Battery, power profile, brightness, suspend |
| radio | provider: media | Live radio (radio-browser.info) |
| rclone | provider: files | Every rclone remote (Drive, Dropbox, OneDrive…) |
| restaurants | front | Find a restaurant, details, how to book |
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

An extension with ready-made setups (`[[recipe]]` in its manifest) names their titles
in its index file too, `recipes = ["Surveiller la boîte mail"]`, so `samantha extension
search` finds them before install.

Installing fetches exactly `commit` from `source`, checks it is what came back, shows
the review (sandbox, tools, hooks) and asks. A release is never edited: a new one is
added. Extensions from other repositories are welcome: open a pull request adding
their index file. Other registries: `samantha registry add <name> <url>[#path]`.

## The site

`python3 scripts/site.py` builds `_site/` (one card per extension: what it does, its
ready-made setups, tools and sandbox, read from the manifest at the pinned commit) and
fails when an index file's `recipes` differ from the manifest's. The Site workflow runs
it on every pull request and publishes it to GitHub Pages from `main` (Settings → Pages
→ Source: GitHub Actions).

## Tests

```
for d in extensions/*/; do (cd "$d" && python3 -m unittest -q); done
```
