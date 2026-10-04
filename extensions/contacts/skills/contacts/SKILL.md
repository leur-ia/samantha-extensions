---
name: contacts
description: The user's address book: find someone's email address or phone number, for a mail, an SMS or a call.
---
# Contacts

- `contacts.search {query, account?, limit?}` → `{contacts: [{id, account, name, emails, phones, organization, kind}], errors?}`. `query` is a name, part of an address or a company. `kind: "other"` is someone the user emailed but never saved.

Answering:
- "Écris à Ana", "envoie un SMS à Marc": search first, then use the address or number found. Several matches: ask which (say their organization or address). None: ask for the address.
- Phone numbers and addresses are personal: give them when asked, don't read whole address books aloud.
- Never invent an address or a number.
