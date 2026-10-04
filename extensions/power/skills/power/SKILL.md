---
name: power
description: Battery, power profile, screen brightness, and locking, suspending or hibernating the computer now or later.
---
# Power

Tools:
- `power.battery {}` → `{present, percent, state, minutes_to_empty, minutes_to_full}`. "Il me reste combien de batterie ?".
- `power.profile {set?}`: `power-saver`, `balanced`, `performance`. "Mode économie" → power-saver.
- `power.brightness {level?, change?}`: 1–100, or +10/−10 ("baisse la luminosité" = −15).
- `power.session {action, in_minutes?}`: `lock`, `suspend`, `hibernate`, now or later ("mets en veille dans 30 minutes"). The user confirms. `power.cancel {}` drops a pending one.

Answering:
- Battery: percent and time left in words ("42 %, environ 2 heures").
- Low battery and the user works on: suggest power-saver and lower brightness.
- Hibernate may be unavailable (no swap big enough): the tool says so; offer suspend.
- If the system refuses an action (polkit), say so plainly.
