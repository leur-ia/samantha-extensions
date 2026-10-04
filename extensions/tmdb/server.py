#!/usr/bin/env python3
"""MCP stdio server of the `tmdb` extension (stdlib only).

A provider of Samantha's `movies` front: The Movie Database API (themoviedb.org, the
user's own API read access token: TMDB_TOKEN, Secret Service item `tmdb`), in the
user's language and country (SAMANTHA_LANGUAGE). Methods: search, ratings, details,
trending, watch (where to watch, data from JustWatch). Ids: `movie/603`, `tv/1399`.
This product uses the TMDB API but is not endorsed or certified by TMDB.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = "2025-06-18"
API = "https://api.themoviedb.org/3"
TIMEOUT = 10
ID = re.compile(r"^(movie|tv)/\d{1,9}$")
LANG = os.environ.get("SAMANTHA_LANGUAGE", "fr")[:2]
LANGUAGE, REGION = {"fr": ("fr-FR", "FR"), "en": ("en-US", "US"), "de": ("de-DE", "DE"),
                    "es": ("es-ES", "ES"), "it": ("it-IT", "IT")}.get(LANG, ("en-US", "US"))
SETUP = ("TMDB n'est pas configuré: crée un compte sur themoviedb.org, copie le « jeton d'accès en "
         "lecture » (Paramètres > API) et enregistre-le avec `samantha provider set-key tmdb`.")


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def api(path, params=None):
    token = os.environ.get("TMDB_TOKEN", "").strip()
    if not token:
        raise Failure(SETUP)
    url = f"{API}{path}?" + urllib.parse.urlencode({"language": LANGUAGE, **(params or {})})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise Failure(SETUP)
        if e.code == 404:
            raise Failure("Titre introuvable sur TMDB")
        raise Failure(f"TMDB a refusé la requête ({e.code})")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise Failure("TMDB injoignable (réseau ?)")
    except ValueError:
        raise Failure("TMDB a répondu quelque chose d'illisible")


def year_of(date):
    return int(date[:4]) if re.match(r"\d{4}", str(date or "")) else None


def item(r, media=None):
    media = media or r.get("media_type")
    if media not in ("movie", "tv"):
        return None
    return {"id": f"{media}/{r['id']}", "title": r.get("title") or r.get("name") or "",
            "year": year_of(r.get("release_date") or r.get("first_air_date")),
            "kind": "series" if media == "tv" else "movie", "overview": (r.get("overview") or "")[:300]}


def media_of(kind):
    if kind in (None, ""):
        return None
    if kind not in ("movie", "series"):
        raise Failure("kind: movie ou series")
    return "tv" if kind == "series" else "movie"


def find(args, key):
    """Search results for the query in `args[key]`."""
    query = str(args.get(key) or "").strip()
    if not query:
        raise Failure("Titre manquant")
    media = media_of(args.get("kind"))
    params = {"query": query, "include_adult": "false"}
    if args.get("year") and media:
        params["year" if media == "movie" else "first_air_date_year"] = int(args["year"])
    found = api(f"/search/{media or 'multi'}", params).get("results", [])
    rows = [r for r in (item(x, media) for x in found) if r]
    if args.get("year") and not media:
        rows = [r for r in rows if r["year"] == int(args["year"])] or rows
    return rows


def search(args):
    limit = max(1, min(20, int(args.get("limit") or 5)))
    return {"results": find(args, "query")[:limit]}


def ratings(args):
    rows = find(args, "title")
    if not rows:
        return {"ratings": []}
    top = rows[0]
    d = api(f"/{top['id']}")
    if not d.get("vote_count"):
        return {"ratings": []}
    return {"ratings": [{"source": "TMDB", "value": f"{d['vote_average']:.1f}/10 ({d['vote_count']} votes)",
                         "title": top["title"], "year": top["year"]}]}


def check(args):
    tid = str(args.get("id") or "")
    if not ID.match(tid):
        raise Failure("id invalide: un id TMDB (movie/… ou tv/…) de movies.search")
    return tid


def details(args):
    tid = check(args)
    d = api(f"/{tid}", {"append_to_response": "credits"})
    crew = (d.get("credits") or {}).get("crew") or []
    director = ", ".join(c["name"] for c in crew if c.get("job") == "Director") or \
        ", ".join(c.get("name", "") for c in d.get("created_by") or [])
    runtime = d.get("runtime") or (d.get("episode_run_time") or [None])[0]
    return {"id": tid, "title": d.get("title") or d.get("name") or "",
            "year": year_of(d.get("release_date") or d.get("first_air_date")),
            "kind": "series" if tid.startswith("tv/") else "movie", "plot": d.get("overview") or "",
            "genres": ", ".join(g["name"] for g in d.get("genres") or []),
            "runtime": f"{runtime} min" if runtime else "", "director": director,
            "cast": ", ".join(c["name"] for c in ((d.get("credits") or {}).get("cast") or [])[:8]),
            "ratings": [{"source": "TMDB", "value": f"{d.get('vote_average', 0):.1f}/10"}] if d.get("vote_count") else []}


def trending(args):
    media = media_of(args.get("kind")) or "all"
    limit = max(1, min(20, int(args.get("limit") or 10)))
    rows = [r for r in (item(x) for x in api(f"/trending/{media}/week").get("results", [])) if r]
    return {"results": rows[:limit]}


def watch(args):
    tid = check(args)
    region = (api(f"/{tid}/watch/providers").get("results") or {}).get(REGION) or {}
    names = lambda k: [p["provider_name"] for p in region.get(k) or []]
    return {"id": tid, "country": REGION, "stream": names("flatrate"), "rent": names("rent"),
            "buy": names("buy"), "free": names("free") + names("ads"), "link": region.get("link", ""),
            "credit": "JustWatch"}


S, I = {"type": "string"}, {"type": "integer"}
TOOLS = {
    "search": (search, "Films and series by title (TMDB).",
               {"type": "object", "required": ["query"], "properties": {"query": S, "year": I, "kind": S, "limit": I}}),
    "ratings": (ratings, "TMDB's rating of a title.",
                {"type": "object", "required": ["title"], "properties": {"title": S, "year": I, "kind": S}}),
    "details": (details, "One title: synopsis, genres, runtime, director, cast.",
                {"type": "object", "required": ["id"], "properties": {"id": S}}),
    "trending": (trending, "Trending films and series this week.",
                 {"type": "object", "properties": {"kind": S, "limit": I}}),
    "watch": (watch, "Where to watch a title in the user's country (JustWatch data).",
              {"type": "object", "required": ["id"], "properties": {"id": S}}),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Réponse de TMDB inattendue ou argument invalide"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out, "isError": False}


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None  # notification
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "samantha-tmdb", "version": "0.1.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": n, "description": d, "inputSchema": s} for n, (_, d, s) in TOOLS.items()]}
    elif method == "tools/call" and params.get("name") in TOOLS:
        result = call(params["name"], params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "method not found"}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            reply = handle(json.loads(line))
        except (ValueError, AttributeError):
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
