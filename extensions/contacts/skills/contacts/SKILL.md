---
name: contacts
description: Use to find someone's email address or phone number in the user's address book or company directory, including before writing, texting, calling or inviting a person named but not addressed ("c'est quoi le mail de Frédéric ?", "écris à Ana").
---
# Contacts

- `contacts.search {query, account?, limit?}` → `{contacts: [{id, account, name, emails, phones, organization, kind}], errors?}`. `query` is a name, part of an address or a company. `kind: "other"` is someone the user emailed but never saved; `kind: "directory"` is a colleague from the company directory (Google Workspace accounts signed in with that access: if a colleague can't be found, suggest `samantha account add google` again).

Answering:
- "Écris à Ana", "envoie un SMS à Marc": search first, then use the address or number found. Several matches: ask which (say their organization or address). None: ask for the address.
- Phone numbers and addresses are personal: give them when asked, don't read whole address books aloud.
- Never invent an address or a number.
