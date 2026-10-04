#!/usr/bin/env python3
"""MCP stdio server of the `calendar` extension (stdlib only).

Calendars in ~/.config/samantha/calendar.toml:

    [accounts.ik]                       # CalDAV (Infomaniak, Zoho, Fastmail, Nextcloud…)
    url = "https://sync.infomaniak.com" # the server; its calendars are discovered
    user = "me@ik.me"                   # password: Secret Service item calendar-ik
    [accounts.google]                   # a read-only iCal feed
    kind = "ics"                        # its secret address: item calendar-google

Secrets come from the daemon (`secrets.get`, `[needs] secrets = ["calendar-*"]`):
never in the file, never logged. CalDAV servers expand recurring events themselves
(`<C:expand>`); feeds are expanded here for the common rules (DAILY, WEEKLY with
BYDAY, MONTHLY, YEARLY; INTERVAL, COUNT, UNTIL, EXDATE, moved instances).
A background loop announces each event 10 minutes before it starts: an island
activity (`calendar.activity`) and a `calendar.soon` event for watchers.
"""

import base64
import datetime
import json
import os
import re
import socket
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zoneinfo

PROTOCOL = "2025-06-18"
TIMEOUT = 15
SOON_MIN = 10
UTC = datetime.timezone.utc
NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}
FRENCH = os.environ.get("SAMANTHA_LANGUAGE", os.environ.get("LANG", "fr")).startswith("fr")
WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def now():
    return datetime.datetime.now(UTC)


# --- config and secrets --------------------------------------------------------------

def accounts():
    home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    try:
        with open(os.path.join(home, "samantha", "calendar.toml"), "rb") as f:
            raw = tomllib.load(f).get("accounts") or {}
    except FileNotFoundError:
        raw = {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise Failure(f"calendar.toml illisible: {e}")
    out = {}
    for name, a in raw.items():
        if not re.match(r"^[a-z0-9-]+$", name) or not isinstance(a, dict):
            continue
        kind = a.get("kind", "caldav")
        if kind == "caldav" and not str(a.get("url", "")).startswith("https://"):
            continue
        out[name] = {"name": name, "kind": kind, "url": str(a.get("url", "")).rstrip("/"),
                     "user": str(a.get("user", ""))}
    if not out:
        raise Failure("Aucun calendrier configuré: ajoute un compte dans "
                      "~/.config/samantha/calendar.toml ([accounts.<nom>] url, user pour CalDAV, "
                      "ou kind = \"ics\" pour une adresse iCal), puis enregistre son mot de passe "
                      "ou son adresse secrète avec `samantha provider set-key calendar-<nom>`.")
    return out


def daemon(method, params, timeout=10):
    path, token = os.environ.get("SAMANTHA_SOCKET"), os.environ.get("SAMANTHA_EXTENSION_TOKEN")
    if not path or not token:
        raise Failure("Le démon de Samantha n'est pas joignable")
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(timeout)
        s.connect(path)
        f = s.makefile("rwb")
        for frame in ({"v": 1, "kind": "hello", "role": "extension", "token": token},
                      {"v": 1, "kind": "request", "id": 1, "method": method, "params": params}):
            f.write(json.dumps(frame).encode() + b"\n")
        f.flush()
        for line in f:
            reply = json.loads(line)
            if reply.get("kind") == "response" and reply.get("id") == 1:
                if not reply.get("ok"):
                    raise Failure(f"démon: {reply.get('error')}")
                return reply["result"]
    raise Failure("Le démon a fermé la connexion")


_secrets = {}


def secret(account):
    name = f"calendar-{account['name']}"
    if name not in _secrets:
        try:
            _secrets[name] = daemon("secrets.get", {"name": name})["value"]
        except Failure:
            raise Failure(f"Secret {name} absent: `samantha provider set-key {name}`")
    return _secrets[name]


def publish(kind, data):
    try:
        daemon("events.publish", {"type": kind, "data": data}, timeout=5)
    except (Failure, OSError, ValueError):
        pass


# --- HTTP / CalDAV -------------------------------------------------------------------

def request(account, method, url, body=None, headers=None, depth=None):
    h = {"User-Agent": "Samantha calendar/0.1", **(headers or {})}
    if account["kind"] == "caldav":
        cred = base64.b64encode(f"{account['user']}:{secret(account)}".encode()).decode()
        h["Authorization"] = f"Basic {cred}"
    if depth is not None:
        h["Depth"] = str(depth)
    if body is not None:
        h.setdefault("Content-Type", "application/xml; charset=utf-8")
    req = urllib.request.Request(url, data=body.encode() if body else None, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read().decode("utf-8", "replace"), r.geturl()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise Failure(f"{account['name']}: identifiants refusés (calendar-{account['name']})")
        if e.code == 412:
            raise Failure("Un évènement avec cet identifiant existe déjà")
        raise Failure(f"{account['name']}: le serveur a refusé la requête ({e.code})")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise Failure(f"{account['name']}: serveur injoignable (réseau ?)")


def href(base, h):
    return urllib.parse.urljoin(base + "/", h)


def propfind(account, url, props, depth):
    body = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            f"<d:prop>{props}</d:prop></d:propfind>")
    text, final = request(account, "PROPFIND", url, body, depth=depth)
    try:
        return ET.fromstring(text), final
    except ET.ParseError:
        raise Failure(f"{account['name']}: réponse CalDAV illisible")


_calendars = {}


def discover(account):
    """[(href, display name)] of an account's calendar collections."""
    if account["name"] in _calendars:
        return _calendars[account["name"]]
    root, base = propfind(account, account["url"] + "/.well-known/caldav",
                          "<d:current-user-principal/>", 0)
    principal = root.find(".//d:current-user-principal/d:href", NS)
    principal = href(base, principal.text) if principal is not None else account["url"]
    root, base = propfind(account, principal, "<c:calendar-home-set/>", 0)
    home = root.find(".//c:calendar-home-set/d:href", NS)
    home = href(base, home.text) if home is not None else principal
    root, base = propfind(account, home, "<d:resourcetype/><d:displayname/>", 1)
    found = []
    for resp in root.findall("d:response", NS):
        if resp.find(".//d:resourcetype/c:calendar", NS) is None:
            continue
        h = href(base, resp.findtext("d:href", "", NS))
        found.append((h, resp.findtext(".//d:displayname", "", NS) or h.rstrip("/").rsplit("/", 1)[-1]))
    _calendars[account["name"]] = found
    return found


def caldav_events(account, start, end):
    fmt = lambda t: t.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    body = ('<?xml version="1.0"?><c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            f'<d:prop><c:calendar-data><c:expand start="{fmt(start)}" end="{fmt(end)}"/></c:calendar-data></d:prop>'
            '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
            f'<c:time-range start="{fmt(start)}" end="{fmt(end)}"/></c:comp-filter></c:comp-filter></c:filter>'
            "</c:calendar-query>")
    events = []
    for url, cal in discover(account):
        text, _ = request(account, "REPORT", url, body, depth=1)
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            raise Failure(f"{account['name']}: réponse CalDAV illisible")
        for data in root.iter("{urn:ietf:params:xml:ns:caldav}calendar-data"):
            for e in expand(parse_ics(data.text or ""), start, end):
                events.append({**e, "calendar": cal, "account": account["name"]})
    return events


# --- iCalendar ---------------------------------------------------------------------

def unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text).splitlines()


def unescape(v):
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), v)


def parse_time(value, params):
    """A datetime (aware) or a date, from an iCalendar value and its parameters."""
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.datetime.strptime(value[:8], "%Y%m%d").date()
    t = datetime.datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return t.replace(tzinfo=UTC)
    try:
        tz = zoneinfo.ZoneInfo(params["TZID"]) if "TZID" in params else None
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        tz = None
    return t.replace(tzinfo=tz) if tz else t.astimezone()  # floating: local time


def parse_ics(text):
    """VEVENTs as dicts: summary, start, end, location, description, uid, rrule, exdates, rid."""
    events, cur = [], None
    for line in unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {"exdates": []}
        elif line == "END:VEVENT" and cur is not None:
            if "start" in cur:
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            head, value = line.split(":", 1)
            name, *rest = head.split(";")
            params = dict(p.split("=", 1) for p in rest if "=" in p)
            try:
                if name == "DTSTART":
                    cur["start"] = parse_time(value, params)
                elif name == "DTEND":
                    cur["end"] = parse_time(value, params)
                elif name == "DURATION":
                    cur["duration"] = parse_duration(value)
                elif name == "RECURRENCE-ID":
                    cur["rid"] = parse_time(value, params)
                elif name == "EXDATE":
                    cur["exdates"] += [parse_time(v, params) for v in value.split(",")]
            except ValueError:
                continue
            if name in ("SUMMARY", "LOCATION", "DESCRIPTION", "UID", "RRULE", "STATUS"):
                cur[name.lower()] = unescape(value) if name != "RRULE" else value
    return events


def parse_duration(v):
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", v)
    if not m:
        raise ValueError(v)
    sign = -1 if m.group(1) == "-" else 1
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    return sign * datetime.timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


def as_dt(t):
    """Dates as local midnight, for comparisons."""
    if isinstance(t, datetime.datetime):
        return t
    return datetime.datetime(t.year, t.month, t.day).astimezone()


def add_months(t, n):
    y, m = divmod(t.month - 1 + n, 12)
    try:
        return t.replace(year=t.year + y, month=m + 1)
    except ValueError:
        return None  # the 31st in a shorter month: no occurrence


def occurrences(e, start, end):
    """Start times of `e` overlapping [start, end) (its RRULE expanded)."""
    first = e["start"]
    length = as_dt(e["end"]) - as_dt(first) if "end" in e else e.get("duration", datetime.timedelta(
        days=1) if not isinstance(first, datetime.datetime) else datetime.timedelta(0))
    rule = dict(p.split("=", 1) for p in e.get("rrule", "").split(";") if "=" in p)
    if not rule:
        return [first] if as_dt(first) < end and as_dt(first) + length > start else []
    freq = rule.get("FREQ")
    step = max(1, int(rule.get("INTERVAL", 1)))
    count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    until = parse_time(rule["UNTIL"], {}) if "UNTIL" in rule else None
    byday = [WEEKDAYS[d[-2:]] for d in rule.get("BYDAY", "").split(",") if d[-2:] in WEEKDAYS]
    excluded = {as_dt(x) for x in e["exdates"]}
    out, n, i = [], 0, 0
    while i < 5000:
        if freq == "DAILY":
            batch = [first + datetime.timedelta(days=i * step)]
        elif freq == "WEEKLY":
            week = first + datetime.timedelta(weeks=i * step)
            monday = week - datetime.timedelta(days=week.weekday())
            batch = sorted(monday + datetime.timedelta(days=d) for d in (byday or [first.weekday()]))
            batch = [b for b in batch if as_dt(b) >= as_dt(first)]
        elif freq == "MONTHLY":
            batch = [b for b in [add_months(first, i * step)] if b]
        elif freq == "YEARLY":
            batch = [b for b in [add_months(first, 12 * i * step)] if b]
        else:
            return [first] if as_dt(first) < end else []
        i += 1
        for t in batch:
            if (until and as_dt(t) > as_dt(until)) or (count is not None and n >= count):
                return out
            n += 1
            if as_dt(t) >= end:
                return out
            if as_dt(t) + length > start and as_dt(t) not in excluded:
                out.append(t)
    return out


def expand(events, start, end):
    """Instances overlapping [start, end): rules expanded, moved instances replacing theirs."""
    moved = {(e.get("uid"), as_dt(e["rid"])) for e in events if "rid" in e}
    out = []
    for e in events:
        if e.get("status") == "CANCELLED":
            continue
        starts = [e["start"]] if "rid" in e else occurrences(e, start, end)
        length = as_dt(e["end"]) - as_dt(e["start"]) if "end" in e else e.get("duration", datetime.timedelta(0))
        for t in starts:
            if "rid" not in e and (e.get("uid"), as_dt(t)) in moved:
                continue
            if "rid" in e and not (as_dt(t) < end and as_dt(t) + length > start):
                continue
            out.append({"uid": e.get("uid", ""), "title": e.get("summary", ""),
                        "start": t, "end": as_dt(t) + length, "all_day": not isinstance(t, datetime.datetime),
                        "location": e.get("location", ""), "description": e.get("description", "")[:500]})
    return out


def ics_events(account, start, end):
    text, _ = request(account, "GET", secret(account))
    return [{**e, "calendar": account["name"], "account": account["name"]}
            for e in expand(parse_ics(text), start, end)]


# --- tools -------------------------------------------------------------------------

def gather(start, end, only=None):
    events, errors = [], []
    for a in accounts().values():
        if only and a["name"] != only:
            continue
        try:
            events += (ics_events if a["kind"] == "ics" else caldav_events)(a, start, end)
        except Failure as e:
            errors.append(str(e))
    events.sort(key=lambda e: as_dt(e["start"]))
    return events, errors


def show(e):
    local = lambda t: as_dt(t).astimezone()
    return {"title": e["title"], "calendar": e["calendar"], "all_day": e["all_day"],
            "start": local(e["start"]).strftime("%Y-%m-%d") if e["all_day"] else local(e["start"]).strftime("%Y-%m-%d %H:%M"),
            "end": local(e["end"]).strftime("%Y-%m-%d %H:%M"), "day": day_name(local(e["start"])),
            "location": e["location"], "description": e["description"]}


def day_name(t):
    today = datetime.datetime.now().astimezone().date()
    delta = (t.date() - today).days
    names = {0: "aujourd'hui", 1: "demain", -1: "hier"} if FRENCH else {0: "today", 1: "tomorrow", -1: "yesterday"}
    if delta in names:
        return names[delta]
    days = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"] if FRENCH else \
           ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    return f"{days[t.weekday()]} {t.day}"


def events(args):
    days = max(1, min(62, int(args.get("days") or 1)))
    local_today = datetime.datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    if args.get("date"):
        try:
            first = datetime.datetime.strptime(str(args["date"]), "%Y-%m-%d").astimezone()
        except ValueError:
            raise Failure("date: AAAA-MM-JJ")
    else:
        first = local_today
    found, errors = gather(first, first + datetime.timedelta(days=days), args.get("account"))
    q = str(args.get("search") or "").lower()
    if q:
        found = [e for e in found if q in f"{e['title']} {e['location']} {e['description']}".lower()]
    out = {"events": [show(e) for e in found][:100]}
    if errors:
        out["errors"] = errors
    return out


def calendars(args):
    rows, errors = [], []
    for a in accounts().values():
        if a["kind"] == "ics":
            rows.append({"account": a["name"], "calendar": a["name"], "writable": False})
            continue
        try:
            rows += [{"account": a["name"], "calendar": name, "writable": True} for _, name in discover(a)]
        except Failure as e:
            errors.append(str(e))
    return {"calendars": rows, **({"errors": errors} if errors else {})}


def ics_text(v):
    return str(v).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def create(args):
    title = str(args.get("title") or "").strip()
    if not title:
        raise Failure("Titre manquant")
    try:
        start = datetime.datetime.strptime(str(args["start"]), "%Y-%m-%d %H:%M").astimezone()
    except (KeyError, ValueError):
        raise Failure("start: AAAA-MM-JJ HH:MM (heure locale)")
    minutes = max(5, min(24 * 60, int(args.get("minutes") or 60)))
    caldav = [a for a in accounts().values() if a["kind"] == "caldav"]
    account = next((a for a in caldav if a["name"] == args.get("account")), caldav[0] if caldav else None)
    if not account:
        raise Failure("Aucun calendrier CalDAV où écrire (les flux iCal sont en lecture seule)")
    cals = discover(account)
    want = str(args.get("calendar") or "").lower()
    url, cal = next(((u, n) for u, n in cals if want and want in n.lower()), cals[0] if cals else (None, None))
    if not url:
        raise Failure(f"{account['name']}: aucun calendrier trouvé")
    uid = f"{uuid.uuid4()}@samantha"
    fmt = lambda t: t.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    body = "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Samantha//calendar//FR", "BEGIN:VEVENT",
        f"UID:{uid}", f"DTSTAMP:{fmt(now())}", f"DTSTART:{fmt(start)}",
        f"DTEND:{fmt(start + datetime.timedelta(minutes=minutes))}", f"SUMMARY:{ics_text(title)}",
        *( [f"LOCATION:{ics_text(args['location'])}"] if args.get("location") else []),
        "END:VEVENT", "END:VCALENDAR", ""])
    request(account, "PUT", href(url, f"{uid}.ics"), body,
            headers={"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"})
    return {"created": title, "calendar": cal, "start": start.strftime("%Y-%m-%d %H:%M"), "minutes": minutes}


# --- reminders ---------------------------------------------------------------------

_announced = set()


def remind_once():
    t0 = now()
    found, _ = gather(t0, t0 + datetime.timedelta(minutes=SOON_MIN + 1))
    for e in found:
        if e["all_day"]:
            continue
        key = f"{e['uid']}:{as_dt(e['start']).isoformat()}"
        lead = (as_dt(e["start"]) - t0).total_seconds() / 60
        if key in _announced or not 0 <= lead <= SOON_MIN:
            continue
        _announced.add(key)
        when = as_dt(e["start"]).astimezone().strftime("%H:%M")
        text = (f"À {when} · " if FRENCH else f"At {when} · ") + e["title"]
        publish("calendar.activity", {"key": key[:120], "text": text, "sub": e["location"],
                                      "icon": "mark", "ttl_s": max(60, int(lead * 60))})
        publish("calendar.soon", {"title": e["title"], "start": when, "location": e["location"],
                                  "calendar": e["calendar"]})


def remind_forever():
    while True:
        try:
            remind_once()
        except (Failure, OSError, ValueError):
            pass
        time.sleep(120)


# --- MCP ---------------------------------------------------------------------------

S, I = {"type": "string"}, {"type": "integer"}
TOOLS = {
    "events": (events, {
        "description": "Events from the user's calendars, soonest first: `days` from `date` "
                       "(default today, 1 day; up to 62), optionally one account or a search on "
                       "title, place and notes. Times are local; `day` is today/tomorrow/weekday.",
        "inputSchema": {"type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD"}, "days": I, "account": S, "search": S}},
        "outputSchema": {"type": "object", "properties": {"events": {"type": "array", "items": {
            "type": "object", "properties": {"title": S, "calendar": S, "start": S, "end": S, "day": S,
                                             "all_day": {"type": "boolean"}, "location": S, "description": S}}},
            "errors": {"type": "array", "items": S}}},
    }),
    "calendars": (calendars, {
        "description": "The user's calendars, by account, and whether events can be added there.",
        "inputSchema": {"type": "object", "properties": {}},
    }),
    "create": (create, {
        "description": "Add an event to a CalDAV calendar: title, start (YYYY-MM-DD HH:MM local), "
                       "minutes (default 60), optional location, account and calendar name.",
        "inputSchema": {"type": "object", "required": ["title", "start"], "properties": {
            "title": S, "start": S, "minutes": I, "location": S, "account": S, "calendar": S}},
    }),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Réponse du calendrier inattendue ou "
                             "argument invalide"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out, "isError": False}


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None  # notification
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "samantha-calendar", "version": "0.1.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": n, **spec} for n, (_, spec) in TOOLS.items()]}
    elif method == "tools/call" and params.get("name") in TOOLS:
        result = call(params["name"], params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": "method not found"}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main():
    if os.environ.get("SAMANTHA_SOCKET"):
        threading.Thread(target=remind_forever, daemon=True).start()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            reply = handle(json.loads(line))
        except (ValueError, AttributeError):
            reply = {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": "parse error"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
