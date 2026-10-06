---
name: backup
description: Use for the user's backups (restic): back up now, is it backed up, when was the last backup, browse snapshots, recover a deleted or overwritten file, a nightly backup ("j'ai effacé mon fichier par erreur"). Cloud storage is files.
---
# Backup

- `backup.status {}` → repository, folders, `latest` snapshot, `running_since`, `last_run` (result of the last backup).
- `backup.backup {}`: starts a backup in the background; the island shows it, `backup.done` says how it went.
- `backup.snapshots {limit?}`, `backup.files {snapshot?, path?}`: browse (snapshot `latest` by default).
- `backup.restore {snapshot?, path}`: restores into `~/Restauré/<date>/`, never over the original (the user confirms).
- `backup.init {}`: creates the repository (first time).

Answering:
- "C'est sauvegardé ?": `backup.status`: date of the latest snapshot, in words ("hier soir à 23 h").
- "J'ai effacé mon fichier X": `backup.files` to find it (path under the user's home), then `backup.restore`, and say where it now is.
- Nightly backups: offer a watcher, `watch.create {trigger: {calendar: "*-*-* 23:00"}, instruction: "Lance backup.backup.", output: "silent"}`; a `backup.done` event with `ok: false` deserves a notification.
- Not set up: pass on the tool's steps (backup.toml, password, init).
