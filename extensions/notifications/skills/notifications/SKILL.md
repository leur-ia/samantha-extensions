---
name: notifications
description: The notifications the user received in the last 7 days: what they missed, from which app, searchable.
---
# Notifications

Tools:
- `notifications.recent {minutes?, app?, new_only?, limit?}` → `{count, notifications: [{time, app, summary, body, urgency}]}`, newest first. `new_only: true` returns what arrived since the last such call, then marks it seen: "Qu'est-ce que j'ai manqué ?".
- `notifications.search {query}`: "la notif avec le code de la banque", "le message de Ana".
- `notifications.apps {}`: who notifies, and how much.
- `notifications.clear {}`: erase the history (asked to the user first).

Answering:
- "Qu'est-ce que j'ai manqué ?": `new_only: true`; group by app, a line per thing that matters, skip noise (repeated downloads, Samantha's own notices). "Rien de nouveau" when empty.
- A verification code or a password in a notification: give it only when the user asks for that code.
- Never invent a notification.
