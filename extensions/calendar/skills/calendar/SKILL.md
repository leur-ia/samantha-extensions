---
name: calendar
description: The user's agenda across their calendars: what's today, tomorrow, this week; find an event; add, move or cancel one; free slots; reminders before meetings.
---
# Calendar

Tools:
Calendars can come from several providers (CalDAV accounts, iCal feeds, Google, Microsoft…); an account is `provider:name` (`caldav:ik`). Every account is covered unless `account` names one.

- `calendar.events {date?, days?, account?, search?}` → `{events: [{id, title, calendar, account, start, end, day, all_day, location, description}], errors?}`, soonest first, local times. `date` is YYYY-MM-DD (default today), `days` 1–62. "Qu'est-ce que j'ai demain ?" → `{date: <tomorrow>}`; "cette semaine" → `{days: 7}`; "quand est mon rendez-vous chez le dentiste ?" → `{days: 62, search: "dentiste"}`.
- `calendar.calendars {}`: the user's calendars and which accept new events.
- `calendar.create {title, start, minutes?, location?, account?, calendar?, attendees?, meet?}`: `start` is "YYYY-MM-DD HH:MM" local. With several providers, give `account` (from `calendar.calendars`, a writable one). `attendees` (email addresses) are invited by the provider, which emails them; `meet: true` adds a Google Meet or Teams link (`meeting_url` in the result). Invitations need a Google or Microsoft account: CalDAV refuses them. Asked to the user first: repeat title, day, time and who is invited.
- `calendar.update {id, event, title?, start?, minutes?, location?}`: move, rename, lengthen or relocate an event. `id` and `event` (its current title) come from `calendar.events`, unchanged; the provider checks they match. A new `start` keeps the length unless `minutes` is given; `location: ""` removes the place. "Décale le dentiste à 15 h" → find it with `calendar.events {days: 62, search: "dentiste"}`, then update. Asked to the user first.
- `calendar.delete {id, event}`: cancel an event, only when the user asks. Asked to the user first. An event with an empty `id` (an iCal feed) can't be changed: say so.
- Recurring events: Google and Outlook change or cancel the one occurrence; CalDAV refuses series ("change-le dans ton agenda").
- `calendar.free {date?, days?, from?, to?, minutes?, with?}` → `{slots: [{date, day, start, end, minutes}], unseen?, errors?}`: free time across every calendar, 09:00–18:00 by default, today from now. "Quand suis-je libre jeudi ?" → `{date: <thursday>}`; "trouve-moi une heure cette semaine" → `{days: 7, minutes: 60}`, then skip the weekend unless asked. Give two or three slots, not the whole list; to book one, `calendar.create`.
- `with`: other people's email addresses; their busy times count too (`calendar.busy` underneath, which shows colleagues in the user's Google Workspace or Microsoft 365 organization). `unseen` people's calendars couldn't be read (outside the organization, not shared): say the slot only fits the user's agenda for them.

Meeting with someone ("trouve un créneau avec Frédéric aujourd'hui et invite-le à un meeting « design »"):
1. `contacts.search {query: "Frédéric"}` for the address; several: ask which (organization, address); none: ask for it. Never guess an address.
2. `calendar.free {with: [address], minutes: <length, default 30>}` (today, or the day said).
3. "Dans la journée" or a clear choice: take the first slot; otherwise offer two or three and wait.
4. `calendar.create {title: "design", start, minutes, attendees: [address], meet: true, account}` on the account of the colleague's organization (the work one, from `calendar.calendars`); the user confirms.
5. Say it in one sentence: "Invitation envoyée à Frédéric pour design, aujourd'hui 14 h 30, avec un lien Meet." 

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
