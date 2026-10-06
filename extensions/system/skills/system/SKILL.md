---
name: system
description: Use to diagnose the computer: slow, hot or noisy, what uses CPU, memory or disk, what runs on a port, a stuck program to stop, failed services, recent errors, a system update waiting for a reboot ("pourquoi mon PC rame ?"). Battery and brightness are power; app updates are flatpak.
---
# System

Tools:
- `system.overview {}` → load (1/5/15 min) and cpus, memory, disks (free GiB, used %), temperatures, uptime.
- `system.processes {sort?, limit?}`: busiest by `cpu` (default) or `memory`, each with its systemd unit (an app's scope, a service).
- `system.process {pid}`: command line, unit, start time, parents, ports.
- `system.ports {port?}`: listening ports and who holds them. "Qu'est-ce qui tourne sur le port 3000 ?".
- `system.stop {pid, force?}`: stops one of the user's processes; confirmed by the user, after you said which program it is.
- `system.services {}`: failed units. `system.errors {minutes?}`: recent errors, grouped with counts.
- `system.updates {}`: the booted system image and a staged update waiting for a reboot (apps: the flatpak extension).

Answering:
- "Pourquoi c'est lent ?": `system.overview` then `system.processes`; name the culprit in plain words (the app, not just its pid) and what you suggest. Load above the cpu count means busy; memory used over 90 % or swap in use means memory pressure.
- "Ça chauffe": temperatures plus top cpu processes. Above 90 °C is hot for a laptop CPU.
- Disk full: which mount, how much is free.
- Errors: explain the few that matter in plain words; ignore known noise (a missing fingerprint PAM module).
- Never stop a process without the user's yes; prefer suggesting to close the app normally.
