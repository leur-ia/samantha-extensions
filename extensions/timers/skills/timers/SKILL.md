---
name: timers
description: Use for timers, alarms and pomodoros that show on the island and ring at the end ("minuteur 10 minutes", "réveille-moi à 7 h"). Calendar events are calendar; "préviens-moi quand…" something happens is a watcher.
---
# Timers

Tools:
- `timers.start {minutes?, seconds?, at?, label?}`: a timer ("10 minutes", "30 secondes") or an alarm (`at` "07:30", the next such time). → `{id, kind, label, ends, remaining_s}`. A short label when the user gives a reason ("pâtes", "réunion").
- `timers.list {}` → `{timers: [{id, kind, label, ends, remaining_s}]}`. "Il reste combien ?".
- `timers.cancel {id}`: by id or label, or `"all"`.

Answering:
- Confirm in a few words with the end time: "C'est parti, 10 minutes, fin à 14 h 10."
- Pomodoro: a 25-minute timer labelled "Pomodoro"; when it ends and the user asks for the break, a 5-minute "Pause".
- "Réveille-moi à 7 h": `at: "07:00"`, label "Réveil".
- Remaining time in words ("encore 4 minutes"), not seconds.
