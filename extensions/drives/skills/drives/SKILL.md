---
name: drives
description: Use for USB sticks, SD cards and external disks plugged into the computer: what's plugged in, mount or open one, safely eject it ("éjecte la clé"). Cloud storage is files; backups are backup.
---
# Drives

Tools (`drive` is a label like "PHOTOS", a device like /dev/sdb1, or the drive's name; omit it when only one is plugged):
- `drives.list {}` → `{drives: [{disk, name, size, connection, partitions: [{device, label, size, filesystem, mounted_at}]}]}`.
- `drives.mount {drive?}` → `{mounted_at}`: the folder to open.
- `drives.eject {drive?}`: unmounts every volume and powers the drive off: safe to unplug.
- `drives.unmount {drive?}`.

Answering:
- "Éjecte la clé": `drives.eject`, then "Tu peux la retirer."
- Busy (a file open, a terminal inside): say so and suggest closing what uses it.
- A drive plugged in shows on the island by itself; mention it only if asked.
