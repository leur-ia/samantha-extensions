#!/usr/bin/env python3
"""MCP stdio server of the `osm` extension (stdlib only).

A provider of Samantha's `restaurants` front from OpenStreetMap: free, no account,
no ratings. The place is found with Nominatim, restaurants around it with Overpass,
both used as their usage policies ask (an identifying User-Agent, a few requests at a
time). Methods: search, details, booking. Data © OpenStreetMap contributors (ODbL).
"""

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = "2025-06-18"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
# Public Overpass instances, tried in turn (the main one is often busy).
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter",
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]
AGENT = "Samantha/0.1 (https://github.com/leur-ia/samantha-extensions; restaurants provider)"
TIMEOUT = 25
RADIUS = 1500
ID = re.compile(r"^(node|way)/\d{1,12}$")
# The user's words for a cuisine → OpenStreetMap `cuisine` values.
CUISINES = {
    "italien": "italian", "italienne": "italian", "pizza": "pizza", "pizzeria": "pizza",
    "japonais": "japanese", "sushi": "sushi", "chinois": "chinese", "indien": "indian",
    "thaï": "thai", "thai": "thai", "vietnamien": "vietnamese", "libanais": "lebanese",
    "mexicain": "mexican", "français": "french", "francais": "french", "niçois": "regional",
    "burger": "burger", "crêperie": "crepe", "creperie": "crepe", "grec": "greek",
    "marocain": "moroccan", "coréen": "korean", "espagnol": "spanish", "tapas": "tapas",
    "fruits de mer": "seafood", "poisson": "seafood", "kebab": "kebab", "végétarien": "vegetarian",
    "vegan": "vegan", "brasserie": "brasserie",
}


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def fetch(url, data=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise Failure("OpenStreetMap limite les requêtes: réessaie dans un moment")
        raise Failure(f"OpenStreetMap a refusé la requête ({e.code})")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise Failure("OpenStreetMap injoignable (réseau ?)")
    except ValueError:
        raise Failure("OpenStreetMap a répondu quelque chose d'illisible")


def locate(near):
    found = fetch(NOMINATIM + "?" + urllib.parse.urlencode({"q": near, "format": "json", "limit": 1}))
    if not found:
        raise Failure(f"Lieu introuvable: {near}")
    return float(found[0]["lat"]), float(found[0]["lon"])


def overpass(query):
    data = urllib.parse.urlencode({"data": f"[out:json][timeout:20];{query}"}).encode()
    last = None
    for url in OVERPASS:
        try:
            return fetch(url, data).get("elements", [])
        except Failure as e:
            last = e  # busy or down: the next instance
    raise last


def escape(s):
    """A user word inside an Overpass regex string: quotes and backslashes dropped,
    regex characters escaped."""
    s = re.sub(r'["\\]', "", s)
    return re.sub(r"[\[\](){}.*+?^$|]", lambda m: "\\\\" + m.group(0), s)


def filter_for(query):
    q = query.lower().strip()
    if not q:
        return ""
    if q in CUISINES:
        value = CUISINES[q]
        if value in ("vegetarian", "vegan"):
            return f'["diet:{value}"~"yes|only"]'
        return f'["cuisine"~"{value}",i]'
    return f'["name"~"{escape(q)}",i]'


def address(t):
    street = " ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street")) if x)
    return ", ".join(x for x in (street, t.get("addr:city")) if x)


def row(e):
    t = e.get("tags") or {}
    kind = e.get("type", "node")
    out = {"id": f"{kind}/{e['id']}", "name": t.get("name", ""), "address": address(t),
           "cuisine": t.get("cuisine", "").replace(";", ", ").replace("_", " "),
           "maps_url": f"https://www.openstreetmap.org/{kind}/{e['id']}"}
    for ours, theirs in (("phone", "phone"), ("website", "website"), ("hours", "opening_hours")):
        value = t.get(theirs) or t.get(f"contact:{theirs}")
        if value:
            out[ours] = value
    return out


def search(args):
    near = str(args.get("near") or "").strip()
    if not near:
        raise Failure("Où ? Donne une ville, un quartier ou une adresse (near)")
    lat, lon = locate(near)
    f = filter_for(str(args.get("query") or ""))
    around = f"(around:{RADIUS},{lat},{lon})"
    found = overpass(f'(node["amenity"="restaurant"]{f}{around};way["amenity"="restaurant"]{f}{around};);out center tags 60;')
    rows = [row(e) for e in found if (e.get("tags") or {}).get("name")]
    limit = max(1, min(20, int(args.get("limit") or 8)))
    return {"results": rows[:limit]}


def element(args):
    oid = str(args.get("id") or "")
    if not ID.match(oid):
        raise Failure("id invalide: un id de restaurants.search (node/… ou way/…)")
    kind, num = oid.split("/")
    found = overpass(f"{kind}({num});out tags;")
    if not found:
        raise Failure("Restaurant introuvable")
    return found[0]


def details(args):
    e = element(args)
    t = e.get("tags") or {}
    out = row(e)
    out.update(reservation=t.get("reservation", ""), terrace=t.get("outdoor_seating"),
               vegetarian=t.get("diet:vegetarian"), wheelchair=t.get("wheelchair"),
               credit="© OpenStreetMap contributors")
    return out


def booking(args):
    e = element(args)
    t = e.get("tags") or {}
    reservation = t.get("reservation", "")
    return {"id": f"{e.get('type')}/{e['id']}", "name": t.get("name", ""),
            "reservable": True if reservation in ("yes", "required", "recommended") else (False if reservation == "no" else None),
            "website": t.get("website") or t.get("contact:website", ""), "phone": t.get("phone") or t.get("contact:phone", ""),
            "maps_url": f"https://www.openstreetmap.org/{e.get('type')}/{e['id']}"}


S, I, B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
TOOLS = {
    "search": (search, "Restaurants near a place (OpenStreetMap).",
               {"type": "object", "required": ["near"], "properties": {"query": S, "near": S, "open_now": B, "limit": I}}),
    "details": (details, "One restaurant: hours, phone, site, terrace, diets.",
                {"type": "object", "required": ["id"], "properties": {"id": S}}),
    "booking": (booking, "How to book: reservation policy, website, phone.",
                {"type": "object", "required": ["id"], "properties": {"id": S}}),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Réponse d'OpenStreetMap inattendue ou argument invalide"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out, "isError": False}


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None  # notification
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "samantha-osm", "version": "0.1.0"}}
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
