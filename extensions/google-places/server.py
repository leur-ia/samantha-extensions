#!/usr/bin/env python3
"""MCP stdio server of the `google-places` extension (stdlib only).

A provider of Samantha's `restaurants` front: Google's Places API (New), with the
user's own API key (GOOGLE_PLACES_KEY, Secret Service item `google-places`; Places API
(New) enabled in their Google Cloud project). Methods: search, details, booking.
Only the fields used are requested (field masks), which keeps calls cheap.
Nothing is logged; no error carries the key.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = "2025-06-18"
API = "https://places.googleapis.com/v1"
TIMEOUT = 10
ID = re.compile(r"^[A-Za-z0-9_-]{10,300}$")
LANG = os.environ.get("SAMANTHA_LANGUAGE", "fr")[:2]
PRICE = {"PRICE_LEVEL_FREE": "gratuit", "PRICE_LEVEL_INEXPENSIVE": "€", "PRICE_LEVEL_MODERATE": "€€",
         "PRICE_LEVEL_EXPENSIVE": "€€€", "PRICE_LEVEL_VERY_EXPENSIVE": "€€€€"}
SEARCH_FIELDS = ",".join("places." + f for f in (
    "id", "displayName", "formattedAddress", "rating", "userRatingCount", "priceLevel",
    "currentOpeningHours.openNow", "primaryTypeDisplayName", "googleMapsUri", "nationalPhoneNumber",
    "websiteUri", "reservable"))
DETAIL_FIELDS = ",".join((
    "id", "displayName", "formattedAddress", "rating", "userRatingCount", "priceLevel",
    "currentOpeningHours.openNow", "regularOpeningHours.weekdayDescriptions", "primaryTypeDisplayName",
    "googleMapsUri", "nationalPhoneNumber", "internationalPhoneNumber", "websiteUri", "reservable",
    "editorialSummary", "servesVegetarianFood", "outdoorSeating", "reviews"))
SETUP = ("Google Places n'est pas configuré: dans ton projet Google Cloud, active « Places API (New) », "
         "crée une clé d'API (restreinte à cette API) et enregistre-la avec "
         "`samantha provider set-key google-places`.")


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def api(method, path, fields, body=None):
    key = os.environ.get("GOOGLE_PLACES_KEY", "").strip()
    if not key:
        raise Failure(SETUP)
    url = API + path + ("" if body else "?" + urllib.parse.urlencode({"languageCode": LANG}))
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": fields,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("error", {}).get("message", "")
        except (ValueError, AttributeError, OSError):
            msg = ""
        if e.code in (401, 403):
            raise Failure(f"Google Places refuse la clé ({e.code}): API non activée, clé restreinte ou "
                          f"facturation non configurée. {msg}".strip())
        if e.code == 404:
            raise Failure("Lieu introuvable")
        raise Failure(f"Google Places a refusé la requête ({e.code}): {msg}".strip())
    except (urllib.error.URLError, TimeoutError, OSError):
        raise Failure("Google Places injoignable (réseau ?)")
    except ValueError:
        raise Failure("Google Places a répondu quelque chose d'illisible")


def row(p):
    out = {"id": p["id"], "name": (p.get("displayName") or {}).get("text", ""), "address": p.get("formattedAddress", ""),
           "cuisine": (p.get("primaryTypeDisplayName") or {}).get("text", ""), "maps_url": p.get("googleMapsUri", "")}
    if p.get("rating") is not None:
        out["rating"], out["reviews"] = p["rating"], p.get("userRatingCount", 0)
    if p.get("priceLevel") in PRICE:
        out["price"] = PRICE[p["priceLevel"]]
    if "openNow" in (p.get("currentOpeningHours") or {}):
        out["open_now"] = p["currentOpeningHours"]["openNow"]
    for ours, theirs in (("phone", "nationalPhoneNumber"), ("website", "websiteUri")):
        if p.get(theirs):
            out[ours] = p[theirs]
    return out


def search(args):
    near = str(args.get("near") or "").strip()
    if not near:
        raise Failure("Où ? Donne une ville, un quartier ou une adresse (near)")
    what = str(args.get("query") or "").strip()
    text = f"{what} restaurant {near}" if what else f"restaurant {near}"
    body = {"textQuery": text, "languageCode": LANG, "includedType": "restaurant",
            "pageSize": max(1, min(20, int(args.get("limit") or 8)))}
    if args.get("open_now"):
        body["openNow"] = True
    found = api("POST", "/places:searchText", SEARCH_FIELDS, body).get("places") or []
    return {"results": [row(p) for p in found]}


def place(args):
    pid = str(args.get("id") or "")
    if not ID.match(pid):
        raise Failure("id invalide: un id de restaurants.search")
    return api("GET", f"/places/{pid}", DETAIL_FIELDS)


def details(args):
    p = place(args)
    out = row(p)
    out.update(hours=(p.get("regularOpeningHours") or {}).get("weekdayDescriptions") or [],
               summary=(p.get("editorialSummary") or {}).get("text", ""),
               reservable=p.get("reservable"), vegetarian=p.get("servesVegetarianFood"),
               terrace=p.get("outdoorSeating"),
               reviews_text=[{"rating": r.get("rating"), "text": ((r.get("text") or {}).get("text") or "")[:300],
                              "when": r.get("relativePublishTimeDescription", "")} for r in (p.get("reviews") or [])[:3]])
    if p.get("internationalPhoneNumber"):
        out["phone_international"] = p["internationalPhoneNumber"]
    return out


def booking(args):
    p = place(args)
    return {"id": p["id"], "name": (p.get("displayName") or {}).get("text", ""), "reservable": p.get("reservable"),
            "website": p.get("websiteUri", ""), "phone": p.get("nationalPhoneNumber", ""),
            "maps_url": p.get("googleMapsUri", "")}


S, I, B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
TOOLS = {
    "search": (search, "Restaurants near a place (Google Places).",
               {"type": "object", "required": ["near"], "properties": {"query": S, "near": S, "open_now": B, "limit": I}}),
    "details": (details, "One restaurant: hours, phone, site, rating, reviews.",
                {"type": "object", "required": ["id"], "properties": {"id": S}}),
    "booking": (booking, "How to book: reservable, website, phone, map.",
                {"type": "object", "required": ["id"], "properties": {"id": S}}),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Réponse de Google Places inattendue ou argument invalide"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out, "isError": False}


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None  # notification
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "samantha-google-places", "version": "0.1.0"}}
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
