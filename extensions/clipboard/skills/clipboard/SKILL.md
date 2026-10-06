---
name: clipboard
description: Use for what the user copied earlier: clipboard history, find something copied before, put it back, summarize or translate it ("le lien que j'ai copié tout à l'heure"). The current selection and clipboard are already in your context.
---
# Clipboard

- `clipboard.history {search?, limit?}` → `{entries: [{index, time, text, chars, truncated}]}`, newest first (index 0 = the last copy; text cut at 300 characters).
- `clipboard.get {index}`: one entry in full. "Résume ce que je viens de copier" → `get {index: 0}`.
- `clipboard.copy {text? | index?}`: puts text (yours: a translation, a cleaned-up version) or an old entry back on the clipboard.
- `clipboard.clear {}`: erase the history (asked to the user first).

Answering:
- "Le lien que j'ai copié tout à l'heure": `history {search: "http"}`, then `copy {index}` and say it's back on the clipboard.
- Passwords copied from a password manager are never in the history; other copies may still be sensitive: quote only what's asked.
- Never invent what was copied.
