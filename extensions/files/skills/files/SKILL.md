---
name: files
description: Use for the user's cloud storage (Google Drive, Dropbox, OneDrive, kDrive…): find a file, browse a folder, download it, upload one ("retrouve le devis sur le Drive"). USB disks are drives; work on local folders is hire.
---
# Files

- `files.places {}` → `{places: [{place, name, kind}]}`: the storages (`rclone:gdrive`…).
- `files.list {place, path?}`: a folder (root by default).
- `files.search {query, place?}` → `{results: [{place, path, size, modified}]}`, every storage unless one is named. Words of the file's name ("devis cuisine").
- `files.download {place, path}` → its local path in Downloads; open it with the desktop or share it (`phone.share`).
- `files.upload {place, file, to?}`: asked to the user first.

Answering:
- "Retrouve le devis de la cuisine": `files.search`, give the best matches with storage and date, offer to download.
- Sizes in Ko/Mo/Go, dates in words.
- No storage yet: the user configures them once in a terminal with `rclone config`.
- Never invent a file.
