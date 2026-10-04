#!/usr/bin/env python3
"""MCP stdio server of the `duffel` extension (stdlib only).

A provider of Samantha's `flights` front: the Duffel API (duffel.com, the user's own
access token: DUFFEL_TOKEN, Secret Service item `duffel`). Searching is free; this
server never books: details carry a link to finish on Google Flights. Methods: places,
search, details. A test token (duffel_test_…) answers with Duffel's sandbox airline.
Nothing is logged; no error carries the token.
"""

import gzip
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = "2025-06-18"
API = "https://api.duffel.com"
TIMEOUT = 40
IATA = re.compile(r"^[A-Z]{3}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
OFFER = re.compile(r"^off_[A-Za-z0-9]+$")
CABINS = ("economy", "premium_economy", "business", "first")
SETUP = ("Duffel n'est pas configuré: crée un compte sur duffel.com, copie un jeton d'accès "
         "(Developers > Access tokens) et enregistre-le avec `samantha provider set-key duffel`.")


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def api(method, path, body=None, query=None):
    token = os.environ.get("DUFFEL_TOKEN", "").strip()
    if not token:
        raise Failure(SETUP)
    url = API + path + (f"?{urllib.parse.urlencode(query)}" if query else "")
    req = urllib.request.Request(url, method=method, data=json.dumps({"data": body}).encode() if body else None,
                                 headers={"Authorization": f"Bearer {token}", "Duffel-Version": "v2",
                                          "Accept": "application/json", "Accept-Encoding": "gzip",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw)["data"]
    except urllib.error.HTTPError as e:
        try:
            errors = json.load(e).get("errors") or [{}]
            msg = errors[0].get("message") or errors[0].get("title") or ""
        except (ValueError, AttributeError, OSError):
            msg = ""
        if e.code == 401:
            raise Failure(SETUP)
        raise Failure(f"Duffel a refusé la requête ({e.code}): {msg}".strip())
    except (urllib.error.URLError, TimeoutError, OSError):
        raise Failure("Duffel injoignable (réseau ?)")
    except (ValueError, KeyError):
        raise Failure("Duffel a répondu quelque chose d'illisible")


def places(args):
    query = str(args.get("query") or "").strip()
    if not query:
        raise Failure("Donne une ville ou un aéroport")
    rows = []
    for p in api("GET", "/places/suggestions", query={"query": query}):
        rows.append({"code": p.get("iata_code") or p.get("iata_city_code") or "", "name": p.get("name", ""),
                     "city": p.get("city_name") or p.get("name", ""), "kind": p.get("type", "")})
    return {"places": [r for r in rows if r["code"]][:10]}


def iso_duration(d):
    """`PT1H35M` → `1 h 35`."""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?", str(d or ""))
    if not m:
        return ""
    days, h, mi = (int(x or 0) for x in m.groups())
    h += 24 * days
    return f"{h} h {mi:02d}" if h else f"{mi} min"


def leg(s):
    segs = s.get("segments") or []
    if not segs:
        return {}
    first, last = segs[0], segs[-1]
    flight = lambda g: f"{(g.get('marketing_carrier') or {}).get('iata_code', '')}{g.get('marketing_carrier_flight_number', '')}"
    return {"from": (first.get("origin") or {}).get("iata_code", ""), "to": (last.get("destination") or {}).get("iata_code", ""),
            "departs": first.get("departing_at", "")[:16].replace("T", " "),
            "arrives": last.get("arriving_at", "")[:16].replace("T", " "),
            "duration": iso_duration(s.get("duration")), "stops": len(segs) - 1,
            "flights": [flight(g) for g in segs]}


def offer_row(o):
    slices = [leg(s) for s in o.get("slices") or []]
    row = {"id": o["id"], "price": float(o.get("total_amount") or 0), "currency": o.get("total_currency", ""),
           "airline": (o.get("owner") or {}).get("name", ""), "stops": max((s.get("stops", 0) for s in slices), default=0),
           "duration": slices[0].get("duration", "") if slices else "", "outbound": slices[0] if slices else {}}
    if len(slices) > 1:
        row["inbound"] = slices[1]
    return row


def search(args):
    origin, dest = str(args.get("from") or "").upper(), str(args.get("to") or "").upper()
    date, back = str(args.get("date") or ""), args.get("return_date")
    if not (IATA.match(origin) and IATA.match(dest)):
        raise Failure("from/to: des codes IATA (NCE, CDG, PAR…): utilise flights.places")
    if not DATE.match(date) or (back and not DATE.match(str(back))):
        raise Failure("Dates: AAAA-MM-JJ")
    cabin = args.get("cabin") or "economy"
    if cabin not in CABINS:
        raise Failure(f"cabin: {', '.join(CABINS)}")
    adults = max(1, min(9, int(args.get("adults") or 1)))
    slices = [{"origin": origin, "destination": dest, "departure_date": date}]
    if back:
        slices.append({"origin": dest, "destination": origin, "departure_date": str(back)})
    body = {"slices": slices, "passengers": [{"type": "adult"}] * adults, "cabin_class": cabin}
    if args.get("max_stops") is not None:
        body["max_connections"] = max(0, min(2, int(args["max_stops"])))
    data = api("POST", "/air/offer_requests", body, query={"return_offers": "true", "supplier_timeout": 20000})
    offers = sorted((offer_row(o) for o in data.get("offers") or []), key=lambda o: o["price"])
    limit = max(1, min(30, int(args.get("limit") or 10)))
    return {"offers": offers[:limit]}


def book_url(row):
    """A Google Flights search for this offer's route and dates (booking happens there)."""
    out = row.get("outbound") or {}
    q = f"Flights from {out.get('from')} to {out.get('to')} on {out.get('departs', '')[:10]}"
    if row.get("inbound"):
        q += f" returning {row['inbound'].get('departs', '')[:10]}"
    return "https://www.google.com/travel/flights?" + urllib.parse.urlencode({"q": q})


def details(args):
    oid = str(args.get("id") or "")
    if not OFFER.match(oid):
        raise Failure("id invalide: un id d'offre (off_…) de flights.search")
    o = api("GET", f"/air/offers/{oid}")
    row = offer_row(o)
    segments = []
    for s in o.get("slices") or []:
        for g in s.get("segments") or []:
            bags = [b for p in g.get("passengers") or [] for b in p.get("baggages") or []]
            segments.append({"flight": f"{(g.get('marketing_carrier') or {}).get('iata_code', '')}{g.get('marketing_carrier_flight_number', '')}",
                             "from": (g.get("origin") or {}).get("iata_code", ""), "to": (g.get("destination") or {}).get("iata_code", ""),
                             "departs": g.get("departing_at", "")[:16].replace("T", " "),
                             "arrives": g.get("arriving_at", "")[:16].replace("T", " "),
                             "aircraft": (g.get("aircraft") or {}).get("name", ""),
                             "bags": ", ".join(f"{b.get('quantity')} {b.get('type')}" for b in bags)})
    cond = o.get("conditions") or {}
    allowed = lambda c: "non" if not (cond.get(c) or {}).get("allowed") else (
        f"oui ({cond[c]['penalty_amount']} {cond[c].get('penalty_currency', '')})" if (cond.get(c) or {}).get("penalty_amount") else "oui")
    return {**row, "segments": segments, "changes": allowed("change_before_departure"),
            "refund": allowed("refund_before_departure"), "expires": o.get("expires_at", ""), "book_url": book_url(row)}


S, I = {"type": "string"}, {"type": "integer"}
TOOLS = {
    "places": (places, "Airports and cities by name, with IATA codes.",
               {"type": "object", "required": ["query"], "properties": {"query": S}}),
    "search": (search, "Flight offers, cheapest first.",
               {"type": "object", "required": ["from", "to", "date"], "properties": {
                   "from": S, "to": S, "date": S, "return_date": S, "adults": I, "cabin": S, "max_stops": I, "limit": I}}),
    "details": (details, "One offer: segments, bags, conditions, book_url.",
                {"type": "object", "required": ["id"], "properties": {"id": S}}),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Réponse de Duffel inattendue ou argument invalide"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out, "isError": False}


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None  # notification
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "samantha-duffel", "version": "0.1.0"}}
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
