#!/usr/bin/env python3
"""MCP stdio server of the `omdb` extension (stdlib only).

A provider of Samantha's `movies` front: the OMDb API (omdbapi.com, an API key from
the user: OMDB_KEY, Secret Service item `omdb`), which carries IMDb, Rotten Tomatoes
and Metacritic ratings. Methods: search, details, ratings. Failures are `isError`
results; nothing is logged and no error carries the key.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = "2025-06-18"
API = "https://www.omdbapi.com/"
TIMEOUT = 10
IMDB = re.compile(r"^tt\d{5,10}$")
KINDS = {"movie": "movie", "series": "series"}
SETUP = ("OMDb n'est pas configuré: demande une clé sur omdbapi.com/apikey.aspx et enregistre-la "
         "avec `samantha provider set-key omdb`.")


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def api(params):
    key = os.environ.get("OMDB_KEY", "").strip()
    if not key:
        raise Failure(SETUP)
    url = API + "?" + urllib.parse.urlencode({**params, "apikey": key})
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise Failure(SETUP if e.code == 401 else f"OMDb a refusé la requête ({e.code})")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise Failure("OMDb injoignable (réseau ?)")
    except ValueError:
        raise Failure("OMDb a répondu quelque chose d'illisible")
    if data.get("Response") == "False":
        err = data.get("Error", "")
        if "API key" in err:
            raise Failure(SETUP)
        return None  # not found
    return data


def year_of(v):
    m = re.match(r"\d{4}", str(v or ""))
    return int(m.group(0)) if m else None


def kind_param(args):
    k = args.get("kind")
    if k and k not in KINDS:
        raise Failure("kind: movie ou series")
    return {"type": KINDS[k]} if k else {}


def search(args):
    query = str(args.get("query") or "").strip()
    if not query:
        raise Failure("Titre manquant")
    params = {"s": query, **kind_param(args)}
    if args.get("year"):
        params["y"] = int(args["year"])
    data = api(params) or {"Search": []}
    limit = max(1, min(10, int(args.get("limit") or 5)))
    return {"results": [{"id": m["imdbID"], "title": m.get("Title", ""), "year": year_of(m.get("Year")),
                         "kind": "series" if m.get("Type") == "series" else "movie"}
                        for m in data.get("Search", [])[:limit] if IMDB.match(m.get("imdbID", ""))]}


def ratings_of(m):
    rows = [{"source": r.get("Source", ""), "value": r.get("Value", "")} for r in m.get("Ratings") or []]
    if not any(r["source"] == "Internet Movie Database" for r in rows) and m.get("imdbRating", "N/A") != "N/A":
        rows.insert(0, {"source": "Internet Movie Database", "value": f"{m['imdbRating']}/10"})
    names = {"Internet Movie Database": "IMDb"}
    return [{"source": names.get(r["source"], r["source"]), "value": r["value"]} for r in rows]


def ratings(args):
    title = str(args.get("title") or "").strip()
    if not title:
        raise Failure("Titre manquant")
    params = {"t": title, **kind_param(args)}
    if args.get("year"):
        params["y"] = int(args["year"])
    m = api(params)
    if not m:
        return {"ratings": []}
    return {"ratings": [{**r, "title": m.get("Title", ""), "year": year_of(m.get("Year"))} for r in ratings_of(m)]}


def details(args):
    imdb = str(args.get("id") or "")
    if not IMDB.match(imdb):
        raise Failure("id invalide: un id IMDb (tt…) de movies.search")
    m = api({"i": imdb, "plot": "full"})
    if not m:
        raise Failure("Titre introuvable")
    na = lambda k: "" if m.get(k) in (None, "N/A") else m[k]
    return {"id": imdb, "title": m.get("Title", ""), "year": year_of(m.get("Year")),
            "kind": "series" if m.get("Type") == "series" else "movie", "plot": na("Plot"),
            "genres": na("Genre"), "runtime": na("Runtime"), "director": na("Director"),
            "cast": na("Actors"), "country": na("Country"), "awards": na("Awards"),
            "ratings": ratings_of(m)}


S, I = {"type": "string"}, {"type": "integer"}
TOOLS = {
    "search": (search, "Films and series by title (OMDb).",
               {"type": "object", "required": ["query"], "properties": {"query": S, "year": I, "kind": S, "limit": I}}),
    "ratings": (ratings, "IMDb, Rotten Tomatoes and Metacritic ratings of a title.",
                {"type": "object", "required": ["title"], "properties": {"title": S, "year": I, "kind": S}}),
    "details": (details, "One title by IMDb id: plot, genres, runtime, director, cast, ratings.",
                {"type": "object", "required": ["id"], "properties": {"id": S}}),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Réponse d'OMDb inattendue ou argument invalide"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out, "isError": False}


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None  # notification
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "samantha-omdb", "version": "0.1.0"}}
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
