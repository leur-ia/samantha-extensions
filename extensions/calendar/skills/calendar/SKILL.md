---
name: calendar
description: The user's agenda across their calendars: what's today, tomorrow, this week; find an event; add one; reminders before meetings.
---
# Calendar

Tools:
Calendars can come from several providers (CalDAV accounts, iCal feeds, Google, Microsoft…); an account is `provider:name` (`caldav:ik`). Every account is covered unless `account` names one.

- `calendar.events {date?, days?, account?, search?}` → `{events: [{title, calendar, account, start, end, day, all_day, location, description}], errors?}`, soonest first, local times. `date` is YYYY-MM-DD (default today), `days` 1–62. "Qu'est-ce que j'ai demain ?" → `{date: <tomorrow>}`; "cette semaine" → `{days: 7}`; "quand est mon rendez-vous chez le dentiste ?" → `{days: 62, search: "dentiste"}`.
- `calendar.calendars {}`: the user's calendars and which accept new events.
- `calendar.create {title, start, minutes?, location?, account?, calendar?}`: `start` is "YYYY-MM-DD HH:MM" local. With several providers, give `account` (from `calendar.calendars`, a writable one). Asked to the user first: repeat title, day and time.

Meetings (like MeetingBar): an event with a video link has `meeting_url` and `meeting` (Teams, Zoom, Google Meet, Webex, Jitsi, kMeet…).
- "Rejoins ma réunion", "lance la visio": `calendar.events` for today, take the event happening now (or the next one within 15 minutes), then `web.open` its `meeting_url`; say "J'ouvre <meeting> pour <title>." No link: say so, and give the place if any.
- "C'est quoi ma prochaine réunion ?": the next event with its time, place and video service.

Answering:
- A day's agenda: times and titles in a short list, the first event highlighted ("Tu commences à 9 h 30 avec le stand-up"). Nothing: "Rien de prévu."
- Resolve relative dates yourself from today's date ("jeudi prochain"), then call with `date`.
- `errors` means an account failed: mention it in one sentence, give the rest.
- Never invent an event or a time. If no calendar is set up, pass on the tool's instructions.
- Each event triggers a reminder on the island 10 minutes before; watchers can use `calendar.soon` events (from every provider) (e.g. "rappelle-moi avec la voix avant chaque réunion").

Example: this week's agenda.

```
root = Surface("center", [list], "Agenda")
week = Query("calendar.events", {days: 7}, {events: []})
list = List(@Each(week.events, "e", Row([Text(e.day, "caption"), Text(e.start, "caption"), Text(e.title)])))
```
