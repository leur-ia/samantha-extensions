---
name: movies
description: Films and series: ratings (IMDb, Rotten Tomatoes, Metacritic, TMDB), what a film is about, what's trending, where to watch it.
---
# Movies

One way to every film provider installed (OMDb: IMDb, Rotten Tomatoes and Metacritic scores; TMDB: details, trends, where to watch). Ids carry their provider (`tmdb:movie/693134`, `omdb:tt15239678`); take them from a call.

- `movies.ratings {title, year?, kind?}` → `{ratings: [{source, value, title, year}], errors?}`: "Dune 2, c'est bien noté ?", "la note Rotten Tomatoes d'Oppenheimer".
- `movies.search {query, year?, kind?}` → `{results: [{id, title, year, kind, overview?}]}`. Several providers may return the same title: treat them as one.
- `movies.details {id}`: synopsis, genres, runtime, director, cast, ratings.
- `movies.trending {kind?}`: this week's films or series.
- `movies.watch {id}` (a TMDB id): streaming, rent, buy in the user's country.

Answering:
- Ratings: give each source with its scale ("IMDb 8,5/10, Rotten Tomatoes 92 %, Metacritic 79/100"), then one sentence of synthesis. Several films match the title: ask which (year) or say which you picked.
- Never invent a score, a cast or a platform. A provider not set up (no key) shows in `errors`: mention it only if nothing else answered.
- Allociné and Rotten Tomatoes have no public API: their pages aren't read; Rotten Tomatoes scores come through OMDb.
- Credit when showing where to watch: "données JustWatch". TMDB data: "This product uses the TMDB API but is not endorsed or certified by TMDB."
