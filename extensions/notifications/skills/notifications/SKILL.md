---
name: notifications
description: Use for the desktop notifications of the last 7 days: what the user missed, from which app, finding one (a code, a message) ("j'avais une notif de la banque", "qu'est-ce que j'ai manqué ?" with watch.inbox). The phone's own notifications are phone.
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
