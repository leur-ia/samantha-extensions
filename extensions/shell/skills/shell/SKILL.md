---
name: shell
description: Use when a request is best done with a command-line tool on the user's files: convert or compress a file (ffmpeg, magick, pandoc), download something (curl, yt-dlp), search or count in folders (rg, fd, du), archives, git, disk usage, a quick check on the machine. Each command is approved by the user first.
---
# Shell

`shell.run {command, cwd?, timeout?}` runs one bash command after the user approves it. It sees the everyday folders (~/Documents, ~/Downloads, ~/Pictures, ~/Music, ~/Videos, ~/Desktop, ~/src, ~/Projects, ~/otto) and the files sent from the app (~/.local/share/otto/uploads, read-only: write results to ~/Downloads). No sudo, no hidden folders, the rest of the home is temporary.

Before running:
- `shell.which {names?}` when unsure a tool is there; `shell.help {program}` before options you don't know by heart. Missing tool: say which and how to get it (a toolbox, flatpak), don't improvise a worse one.
- One command does one clear thing. Prefer reading (ls, du, rg, file, ffprobe) before changing; never delete, overwrite or move many files unless the user asked exactly that, and then say what will go (`-i`-free commands are fine since the user approves, but name the files).
- Explain the command in one short sentence of plain words when asking for approval; the user sees the command too.

Choosing:
- Several calls to Otto's own tools chained together: code mode (code.run), not the shell.
- Real work on a code project (edit many files, run its tests): hire an agent (agent.hire).
- Long jobs (a big download, a long encode): set `timeout` (up to 600 s); beyond that, say it would need an agent or a watcher.

After running: say what changed or what was found in a sentence or two, with the output file's path; on a non-zero exit, read stderr and say why in plain words. Never claim a command succeeded without its result.
