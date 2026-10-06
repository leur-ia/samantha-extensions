---
name: phone
description: Use for the user's phone through KDE Connect: ring it to find it, its battery, what it shows, send an SMS, send it a file or a link, pair it ("où est mon téléphone ?", "envoie cette page sur mon portable").
---
# Phone

Tools (`device` defaults to the paired, reachable phone):
- `phone.ring {}`: "Où est mon téléphone ?", "fais sonner mon portable".
- `phone.battery {}` → `{device, charge, charging}`.
- `phone.notifications {}` → `{notifications: [{app, text}]}`: what the phone shows now.
- `phone.send_sms {to, text}`: a number in international or local form. The user confirms; repeat recipient and text. A name, not a number ("ma soeur", "Marc"): `contacts.search` if installed; several matches, ask which; none, ask for the number.
- `phone.share {what}`: an absolute file path or a link ("envoie cette page sur mon téléphone").
- `phone.message {text}`: a short note shown on the phone.
- `phone.devices {refresh?}`, `phone.pair {device?}`: setup.

Answering:
- Short confirmations ("Ton Pixel sonne.", "64 %, en charge.").
- Not reachable: say the phone must be on the same Wi-Fi with KDE Connect running.
- No phone paired: explain the KDE Connect app setup, then `phone.pair` (accepted on the phone).
