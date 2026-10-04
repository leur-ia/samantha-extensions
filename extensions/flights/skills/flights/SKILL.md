---
name: flights
description: Find and compare flights: airports, offers by price and duration, an offer's conditions, and where to book it.
---
# Flights

Samantha searches and compares; the user books on the site you open for them (never pay or book yourself).

- `flights.places {query}` → `{places: [{code, name, city, kind}]}`: turn "Nice", "Paris" into IATA codes (a city code like PAR covers all its airports).
- `flights.search {from, to, date, return_date?, adults?, cabin?, max_stops?}` → `{offers: [{id, price, currency, airline, stops, duration, outbound, inbound?}], errors?}`, cheapest first.
- `flights.details {id}`: segments, baggage, change/refund conditions, and `book_url`.

Answering:
- "Un vol Nice–Paris vendredi": resolve the date yourself, `flights.places` if a code is unknown, then `flights.search`. Give the 3 best: price, airline, departure–arrival times, direct or stops; say which is cheapest and which is fastest.
- Several airports or flexible dates ("le week-end prochain"): search a few dates and compare.
- To book: `flights.details`, then `web.open` its `book_url` and say the price may move on the site.
- Never invent a flight, a price or a time. A provider in `errors` (not set up): mention it only if nothing answered.
