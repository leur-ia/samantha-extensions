---
name: restaurants
description: Find a restaurant: near a place, by cuisine, open now; ratings, hours, phone, and how to book a table.
---
# Restaurants

Samantha finds and compares; the user books (a page you open, or a number they call).

- `restaurants.search {near, query?, open_now?, limit?}` → `{results: [{id, name, address, cuisine, rating?, reviews?, price?, open_now?, phone?, website?, maps_url}], errors?}`. `near` is required: a city, a neighbourhood or an address. When the user doesn't say where, use their city (the weather extension's default place, or ask).
- `restaurants.details {id}`: hours, phone, website, map, rating, a few reviews, whether it takes reservations.
- `restaurants.booking {id}` → `{reservable, booking_url?, website?, phone?, maps_url}`.

Answering:
- "Un bon italien ce soir à Nice": `search {near: "Nice", query: "italien", open_now: true}`; give 3: name, rating and number of reviews, price, neighbourhood. Several providers may list the same place: one line each.
- "Réserve chez X": `restaurants.booking`, then `web.open` the booking page (or the website), or give the phone number; say you can't book yourself.
- To share it on the user's phone: the `phone` extension's share, if installed.
- Never invent a rating, an opening hour or a number. A provider in `errors` (no key): mention it only if nothing answered.
