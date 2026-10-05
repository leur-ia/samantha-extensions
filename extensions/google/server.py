#!/usr/bin/env python3
"""MCP stdio server of the `google` extension (stdlib only).

A provider (Samantha's fronts serve it) for the user's Google accounts: Gmail (`mail`),
Google Calendar (`calendar`), Google Contacts (`contacts`). Run as `server.py mail`,
`server.py calendar` or `server.py contacts`. Access tokens come from Samantha's daemon
(`oauth.token`; the user signed in with `samantha account add google`, with their own
OAuth client); no password or refresh token reaches this server, and nothing is
logged. The calendar server also announces events 10 minutes before they start
(island activity `google.activity`, event `calendar.soon`).

Ids carry their account: `me@gmail.com/18c2…` (an address has no `/`).
"""

import base64
import datetime
import email.message
import email.policy
import email.utils
import html
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

PROTOCOL = "2025-06-18"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR = "https://www.googleapis.com/calendar/v3"
PEOPLE = "https://people.googleapis.com/v1"
TIMEOUT = 20
MAX_LIST = 50
MAX_BODY = 20000
SOON_MIN = 10
FRENCH = os.environ.get("SAMANTHA_LANGUAGE", os.environ.get("LANG", "fr")).startswith("fr")
SETUP = ("Aucun compte Google: mets le client_id de ton app dans [oauth.google] de config.toml, "
         "puis `samantha account add google`.")


class Failure(Exception):
    """A message for the model: the call failed, say why."""


class Denied(Failure):
    """Google answered 401 or 403."""


# --- daemon and Google APIs --------------------------------------------------------

def daemon(method, params, timeout=30):
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
                    err = str(reply.get("error", ""))
                    raise Failure(SETUP if "no google account" in err else f"Compte Google: {err}")
                return reply["result"]
    raise Failure("Le démon a fermé la connexion")


DIRECTORY = "https://www.googleapis.com/auth/directory.readonly"


def accounts():
    names = daemon("oauth.accounts", {"provider": "google"})["accounts"]
    if not names:
        raise Failure(SETUP)
    return names


def granted(account, scope):
    """Whether `account` granted `scope` at sign-in (older daemons don't say: assume so)."""
    scopes = daemon("oauth.accounts", {"provider": "google"}).get("scopes")
    return scopes is None or scope in scopes.get(account, [])


EMAIL = re.compile(r"[^@\s,<>]+@[^@\s,<>]+\.[^@\s,<>]+")


def emails(value):
    """A list of addresses (or one comma-separated string), each checked."""
    items = value.split(",") if isinstance(value, str) else list(value or [])
    out = [str(e).strip() for e in items if str(e).strip()]
    bad = [e for e in out if not EMAIL.fullmatch(e)]
    if bad:
        raise Failure(f"Adresse invalide: {', '.join(bad)} (cherche-la avec contacts.search)")
    return out


def api(account, method, url, body=None, query=None):
    token = daemon("oauth.token", {"provider": "google", "account": account})["access_token"]
    if query:
        url += "?" + urllib.parse.urlencode(query, doseq=True)
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("error", {}).get("message", "")
        except (ValueError, AttributeError, OSError):
            msg = ""
        if e.code in (401, 403):
            raise Denied(f"Google refuse l'accès ({e.code}): API non activée dans ton projet Google Cloud, "
                          f"ou consentement à renouveler (`samantha account add google`). {msg}".strip())
        if e.code == 404:
            raise Failure("Introuvable chez Google")
        raise Failure(f"Google a refusé la requête ({e.code}): {msg}".strip())
    except (urllib.error.URLError, TimeoutError, OSError):
        raise Failure("Google injoignable (réseau ?)")
    except ValueError:
        raise Failure("Google a répondu quelque chose d'illisible")


def chosen(args):
    names = accounts()
    want = args.get("account")
    if want in (None, "", "all"):
        return names
    if want not in names:
        raise Failure(f"Pas de compte Google {want!r}: {', '.join(names)}")
    return [want]


def split_id(value):
    account, _, native = str(value or "").partition("/")
    if not account or not native or not re.fullmatch(r"[\w.@+-]+", native):
        raise Failure("id invalide: prends l'id renvoyé par la liste")
    return account, native


def local_zone():
    try:
        return os.path.realpath("/etc/localtime").split("zoneinfo/", 1)[1]
    except (IndexError, OSError):
        return "UTC"


def when(iso, now=None):
    try:
        t = datetime.datetime.fromisoformat(iso).astimezone()
    except (TypeError, ValueError):
        return iso
    now = now or datetime.datetime.now().astimezone()
    days = (now.date() - t.date()).days
    months = (["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
              if FRENCH else ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])
    if days == 0:
        return t.strftime("%H:%M")
    if days == 1:
        return ("hier " if FRENCH else "yesterday ") + t.strftime("%H:%M")
    day = f"{t.day} {months[t.month - 1]}" if FRENCH else f"{months[t.month - 1]} {t.day}"
    return day if t.year == now.year else f"{day} {t.year}"


# --- Gmail -------------------------------------------------------------------------

def headers_of(m):
    return {h["name"].lower(): h["value"] for h in (m.get("payload") or {}).get("headers") or []}


def mail_row(account, m):
    h = headers_of(m)
    name, addr = email.utils.parseaddr(h.get("from", ""))
    stamp = int(m.get("internalDate", "0")) / 1000
    date = datetime.datetime.fromtimestamp(stamp).astimezone().isoformat(timespec="seconds")
    return {"id": f"{account}/{m['id']}", "account": account, "from": addr.lower(), "name": name or addr,
            "subject": h.get("subject", ""), "date": date, "when": when(date),
            "unread": "UNREAD" in (m.get("labelIds") or [])}


def gmail_query(query):
    """The mail capability's query syntax in Gmail's: since:/before: → after:/before:."""
    out = []
    for word in str(query).split():
        for ours, theirs in (("since:", "after:"), ("before:", "before:")):
            if word.startswith(ours):
                word = theirs + word[len(ours):].replace("-", "/")
        out.append(word)
    return " ".join(out)


def listing(args, q):
    limit = max(1, min(MAX_LIST, int(args.get("limit") or 20)))
    rows, errors = [], []
    folder = str(args.get("folder") or "INBOX").upper()
    for account in chosen(args):
        try:
            params = {"maxResults": limit, "q": q}
            if folder and not q:
                params["labelIds"] = folder
            ids = api(account, "GET", f"{GMAIL}/messages", query=params).get("messages", [])
            for m in ids:
                full = api(account, "GET", f"{GMAIL}/messages/{m['id']}",
                           query={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]})
                rows.append(mail_row(account, full))
        except Failure as e:
            errors.append(f"{account}: {e}")
    rows.sort(key=lambda r: r["date"], reverse=True)
    out = {"rows": rows[:limit]}
    if errors:
        out["errors"] = errors
    return out


def mail_accounts(args):
    return {"rows": [{"account": n, "label": "", "address": n, "default": i == 0} for i, n in enumerate(accounts())]}


def mail_list(args):
    q = "is:unread" if args.get("unread_only") else ""
    if q and args.get("folder"):
        q += f" label:{str(args['folder']).lower()}"
    elif q:
        q += " in:inbox"
    return listing(args, q)


def mail_search(args):
    query = str(args.get("query") or "").strip()
    if not query:
        raise Failure("Recherche vide")
    return listing(args, gmail_query(query))


def b64decode(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def text_of(part):
    """The message's text: its text/plain part, else its HTML reduced to text."""
    plain, rich = [], []

    def walk(p):
        mime, data = p.get("mimeType", ""), (p.get("body") or {}).get("data")
        if data and mime == "text/plain":
            plain.append(b64decode(data))
        elif data and mime == "text/html":
            rich.append(b64decode(data))
        for child in p.get("parts") or []:
            walk(child)
    walk(part)
    if plain:
        return "\n".join(plain)
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", "\n".join(rich))
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def attachments_of(part):
    out = []
    for p in part.get("parts") or []:
        if p.get("filename"):
            out.append({"name": p["filename"], "size": (p.get("body") or {}).get("size", 0)})
        out += attachments_of(p)
    return out


def mail_read(args):
    account, native = split_id(args.get("id"))
    m = api(account, "GET", f"{GMAIL}/messages/{native}", query={"format": "full"})
    body = text_of(m.get("payload") or {})
    h = headers_of(m)
    out = mail_row(account, m)
    out.update(to=h.get("to", ""), cc=h.get("cc", ""), body=body[:MAX_BODY], truncated=len(body) > MAX_BODY,
               attachments=attachments_of(m.get("payload") or {}))
    return out


def mail_mark_read(args):
    account, native = split_id(args.get("id"))
    read = args.get("read", True) is not False
    api(account, "POST", f"{GMAIL}/messages/{native}/modify",
        {"removeLabelIds": ["UNREAD"]} if read else {"addLabelIds": ["UNREAD"]})
    return {"id": f"{account}/{native}", "read": read}


def raw_message(account, args):
    msg = email.message.EmailMessage(policy=email.policy.SMTP)
    msg["From"], msg["To"], msg["Subject"] = account, str(args.get("to") or ""), str(args.get("subject") or "")
    if "@" not in msg["To"]:
        raise Failure("Destinataire invalide")
    msg.set_content(str(args.get("body") or ""))
    thread = None
    if args.get("reply_to_id"):
        _, native = split_id(args["reply_to_id"])
        orig = api(account, "GET", f"{GMAIL}/messages/{native}",
                   query={"format": "metadata", "metadataHeaders": ["Message-ID", "References"]})
        h = headers_of(orig)
        if h.get("message-id"):
            msg["In-Reply-To"] = h["message-id"]
            msg["References"] = f"{h.get('references', '')} {h['message-id']}".strip()
        thread = orig.get("threadId")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return {"raw": raw, **({"threadId": thread} if thread else {})}


def sending_account(args):
    if args.get("reply_to_id"):
        return split_id(args["reply_to_id"])[0]
    names = accounts()
    account = args.get("account") or names[0]
    if account not in names:
        raise Failure(f"Pas de compte Google {account!r}: {', '.join(names)}")
    return account


def mail_draft(args):
    account = sending_account(args)
    d = api(account, "POST", f"{GMAIL}/drafts", {"message": raw_message(account, args)})
    return {"draft": True, "account": account, "id": f"{account}/{(d.get('message') or {}).get('id', '')}"}


def mail_send(args):
    account = sending_account(args)
    m = api(account, "POST", f"{GMAIL}/messages/send", raw_message(account, args))
    return {"sent": True, "account": account, "id": f"{account}/{m.get('id', '')}"}


# --- Calendar ----------------------------------------------------------------------

def day_name(t):
    today = datetime.datetime.now().astimezone().date()
    delta = (t.date() - today).days
    names = {0: "aujourd'hui", 1: "demain", -1: "hier"} if FRENCH else {0: "today", 1: "tomorrow", -1: "yesterday"}
    if delta in names:
        return names[delta]
    days = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"] if FRENCH else \
        ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    return f"{days[t.weekday()]} {t.day}"


MEETINGS = [(r"zoom\.us/(?:j|my|w)/", "Zoom"), (r"meet\.google\.com/[a-z]{3}-", "Google Meet"),
            (r"teams\.(?:microsoft|live)\.com/", "Teams"), (r"webex\.com/", "Webex"),
            (r"meet\.jit\.si/", "Jitsi"), (r"whereby\.com/", "Whereby"), (r"kmeet\.infomaniak\.com/", "kMeet")]


def meeting(e):
    """The event's video link: Google's conference data first, else a link in its text."""
    if e.get("hangoutLink"):
        return {"meeting_url": e["hangoutLink"], "meeting": "Google Meet"}
    for ep in (e.get("conferenceData") or {}).get("entryPoints") or []:
        if ep.get("entryPointType") == "video" and ep.get("uri"):
            name = ((e.get("conferenceData") or {}).get("conferenceSolution") or {}).get("name") or "Visio"
            return {"meeting_url": ep["uri"], "meeting": name}
    for url in re.findall(r"https://[^\s<>\"']+", f"{e.get('location', '')} {e.get('description', '')}"):
        for pattern, service in MEETINGS:
            if re.search(pattern, url):
                return {"meeting_url": url.rstrip(").,;"), "meeting": service}
    return {"meeting_url": "", "meeting": ""}


def calendars_of(account):
    items = api(account, "GET", f"{CALENDAR}/users/me/calendarList").get("items", [])
    return [c for c in items if not c.get("hidden")]


def parse_when(value):
    if "dateTime" in value:
        return datetime.datetime.fromisoformat(value["dateTime"]).astimezone(), False
    d = datetime.date.fromisoformat(value["date"])
    return datetime.datetime(d.year, d.month, d.day).astimezone(), True


def events_between(account, first, last):
    out = []
    for cal in calendars_of(account):
        if not cal.get("selected", True):
            continue
        cid = urllib.parse.quote(cal["id"], safe="")
        items = api(account, "GET", f"{CALENDAR}/calendars/{cid}/events", query={
            "timeMin": first.isoformat(), "timeMax": last.isoformat(), "singleEvents": "true",
            "orderBy": "startTime", "maxResults": 250, "timeZone": local_zone()}).get("items", [])
        for e in items:
            if e.get("status") == "cancelled":
                continue
            start, all_day = parse_when(e.get("start") or {})
            end, _ = parse_when(e.get("end") or {})
            out.append({"id": f"{account}/{cal['id']}/{e.get('id', '')}", "title": e.get("summary") or "", "calendar": cal.get("summary", ""),
                        "account": account, "all_day": all_day,
                        "start": start.strftime("%Y-%m-%d") if all_day else start.strftime("%Y-%m-%d %H:%M"),
                        "end": end.strftime("%Y-%m-%d %H:%M"), "day": day_name(start),
                        "location": e.get("location") or "", "description": (e.get("description") or "")[:500],
                        **meeting(e), "_start": start})
    return out


def calendar_events(args):
    days = max(1, min(62, int(args.get("days") or 1)))
    try:
        first = (datetime.datetime.strptime(str(args["date"]), "%Y-%m-%d") if args.get("date")
                 else datetime.datetime.now()).replace(hour=0, minute=0, second=0, microsecond=0).astimezone()
    except ValueError:
        raise Failure("date: AAAA-MM-JJ")
    last = first + datetime.timedelta(days=days)
    search = str(args.get("search") or "").lower()
    events, errors = [], []
    for account in chosen(args):
        try:
            events += events_between(account, first, last)
        except Failure as e:
            errors.append(f"{account}: {e}")
    events = [e for e in events if not search or search in f"{e['title']} {e['location']} {e['description']}".lower()]
    events.sort(key=lambda e: e["_start"])
    for e in events:
        del e["_start"]
    return {"events": events, **({"errors": errors} if errors else {})}


def calendar_calendars(args):
    rows, errors = [], []
    for account in chosen(args):
        try:
            rows += [{"account": account, "calendar": c.get("summary", ""),
                      "writable": c.get("accessRole") in ("owner", "writer")} for c in calendars_of(account)]
        except Failure as e:
            errors.append(f"{account}: {e}")
    return {"calendars": rows, **({"errors": errors} if errors else {})}


def calendar_create(args):
    title = str(args.get("title") or "").strip()
    if not title:
        raise Failure("Titre manquant")
    try:
        start = datetime.datetime.strptime(str(args["start"]), "%Y-%m-%d %H:%M")
    except (KeyError, ValueError):
        raise Failure("start: AAAA-MM-JJ HH:MM (heure locale)")
    minutes = max(5, min(24 * 60, int(args.get("minutes") or 60)))
    names = accounts()
    account = args.get("account") or names[0]
    if account not in names:
        raise Failure(f"Pas de compte Google {account!r}: {', '.join(names)}")
    cid = "primary"
    if args.get("calendar"):
        match = [c for c in calendars_of(account) if str(args["calendar"]).lower() in c.get("summary", "").lower()
                 and c.get("accessRole") in ("owner", "writer")]
        if not match:
            raise Failure("Calendrier introuvable ou en lecture seule")
        cid = urllib.parse.quote(match[0]["id"], safe="")
    zone = local_zone()
    event = {"summary": title, "start": {"dateTime": start.isoformat(), "timeZone": zone},
             "end": {"dateTime": (start + datetime.timedelta(minutes=minutes)).isoformat(), "timeZone": zone}}
    if args.get("location"):
        event["location"] = str(args["location"])
    guests = emails(args.get("attendees"))
    if guests:
        event["attendees"] = [{"email": e} for e in guests]
    if args.get("meet"):
        event["conferenceData"] = {"createRequest": {"requestId": uuid.uuid4().hex,
                                                     "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
    # Google sends the invitations (sendUpdates=all); the Meet link needs conferenceDataVersion.
    made = api(account, "POST", f"{CALENDAR}/calendars/{cid}/events", event,
               query={"sendUpdates": "all" if guests else "none", "conferenceDataVersion": 1})
    out = {"created": title, "account": account, "start": start.strftime("%Y-%m-%d %H:%M"), "minutes": minutes}
    if guests:
        out["attendees"] = guests
    if made.get("hangoutLink"):
        out["meeting_url"] = made["hangoutLink"]
    return out


def calendar_busy(args):
    """Other people's busy periods (Google's free/busy: colleagues in the same Workspace)."""
    people = emails(args.get("emails"))
    if not people:
        raise Failure("emails: les adresses des personnes")
    days = max(1, min(14, int(args.get("days") or 1)))
    try:
        first = (datetime.datetime.strptime(str(args["date"]), "%Y-%m-%d") if args.get("date")
                 else datetime.datetime.now()).replace(hour=0, minute=0, second=0, microsecond=0).astimezone()
    except ValueError:
        raise Failure("date: AAAA-MM-JJ")
    rows, errors = [], []
    for account in chosen(args):
        try:
            got = api(account, "POST", f"{CALENDAR}/freeBusy", {
                "timeMin": first.isoformat(), "timeMax": (first + datetime.timedelta(days=days)).isoformat(),
                "timeZone": local_zone(), "items": [{"id": e} for e in people]}).get("calendars", {})
        except Failure as e:
            errors.append(f"{account}: {e}")
            continue
        for e in people:
            cal = got.get(e) or {}
            if cal.get("errors") or e not in got:
                reason = ((cal.get("errors") or [{}])[0]).get("reason", "notFound")
                rows.append({"email": e, "seen": False, "reason": f"{account}: {reason}"})
                continue
            rows.append({"email": e, "seen": True})
            for b in cal.get("busy", []):
                start = datetime.datetime.fromisoformat(b["start"].replace("Z", "+00:00")).astimezone()
                end = datetime.datetime.fromisoformat(b["end"].replace("Z", "+00:00")).astimezone()
                rows.append({"email": e, "start": start.strftime("%Y-%m-%d %H:%M"), "end": end.strftime("%Y-%m-%d %H:%M")})
    return {"busy": rows, **({"errors": errors} if errors else {})}


def event_ref(value):
    """(account, quoted calendar id, event id) of an id from calendar.events."""
    account, _, rest = str(value or "").partition("/")
    cal, _, eid = rest.rpartition("/")
    if not re.fullmatch(r"[\w.@+#-]+", cal) or not cal.strip(".") or not re.fullmatch(r"\w+", eid):
        raise Failure("id invalide: prends l'id renvoyé par calendar.events")
    if account not in accounts():
        raise Failure(f"Pas de compte Google {account!r}")
    return account, urllib.parse.quote(cal, safe=""), eid


def checked_event(args):
    """The event behind args["id"], refused unless its title is args["event"]: the user
    confirmed that title, so the id must not name another event."""
    account, cal, eid = event_ref(args.get("id"))
    url = f"{CALENDAR}/calendars/{cal}/events/{eid}"
    e = api(account, "GET", url, query={"timeZone": local_zone()})
    title = e.get("summary") or ""
    if title.strip().lower() != str(args.get("event") or "").strip().lower():
        raise Failure(f"Cet id est l'évènement « {title} », pas « {args.get('event')} »: reprends l'id dans calendar.events")
    return account, url, e


def calendar_update(args):
    account, url, e = checked_event(args)
    patch = {}
    if str(args.get("title") or "").strip():
        patch["summary"] = str(args["title"]).strip()
    if args.get("location") is not None:
        patch["location"] = str(args["location"])
    if args.get("start") or args.get("minutes"):
        old_start, all_day = parse_when(e.get("start") or {})
        old_end, _ = parse_when(e.get("end") or {})
        # ponytail: all-day events aren't moved here; add date-only starts if asked for.
        if all_day:
            raise Failure("Évènement sur la journée entière: je ne sais changer que les heures")
        try:
            start = (datetime.datetime.strptime(str(args["start"]), "%Y-%m-%d %H:%M") if args.get("start")
                     else old_start.replace(tzinfo=None))
        except ValueError:
            raise Failure("start: AAAA-MM-JJ HH:MM (heure locale)")
        length = (datetime.timedelta(minutes=max(5, min(24 * 60, int(args["minutes"])))) if args.get("minutes")
                  else old_end - old_start)
        zone = local_zone()
        patch["start"] = {"dateTime": start.isoformat(), "timeZone": zone}
        patch["end"] = {"dateTime": (start + length).isoformat(), "timeZone": zone}
    if not patch:
        raise Failure("Rien à changer: donne title, start, minutes ou location")
    api(account, "PATCH", url, patch)
    return {"updated": patch.get("summary", e.get("summary") or ""), "id": args["id"], "account": account,
            **({"start": patch["start"]["dateTime"][:16].replace("T", " ")} if "start" in patch else {})}


def calendar_delete(args):
    account, url, e = checked_event(args)
    api(account, "DELETE", url)
    return {"deleted": e.get("summary") or "", "account": account}


_announced = set()


def publish(kind, data):
    try:
        daemon("events.publish", {"type": kind, "data": data}, timeout=5)
    except (Failure, OSError, ValueError):
        pass


def remind_once():
    now = datetime.datetime.now().astimezone()
    for account in accounts():
        for e in events_between(account, now, now + datetime.timedelta(minutes=SOON_MIN + 1)):
            lead = (e["_start"] - now).total_seconds() / 60
            key = f"{account}:{e['id']}:{e['_start'].isoformat()}"
            if e["all_day"] or key in _announced or not 0 <= lead <= SOON_MIN:
                continue
            _announced.add(key)
            at = e["_start"].strftime("%H:%M")
            text = (f"À {at} · " if FRENCH else f"At {at} · ") + e["title"] + (f" · {e['meeting']}" if e["meeting"] else "")
            publish("google.activity", {"key": key[:120], "text": text, "sub": e["location"], "ttl_s": max(60, int(lead * 60))})
            publish("calendar.soon", {"title": e["title"], "start": at, "location": e["location"],
                                      "calendar": e["calendar"], "meeting_url": e["meeting_url"]})


def remind_forever():
    while True:
        try:
            remind_once()
        except (Failure, OSError, ValueError, KeyError):
            pass
        time.sleep(120)


# --- Contacts ----------------------------------------------------------------------

READ_MASK = "names,emailAddresses,phoneNumbers,organizations"


def person(account, p, kind="contact"):
    name = ((p.get("names") or [{}])[0]).get("displayName", "")
    org = ((p.get("organizations") or [{}])[0]).get("name", "")
    return {"id": f"{account}/{p.get('resourceName', '').replace('/', '.')}", "account": account, "name": name,
            "emails": [e["value"] for e in p.get("emailAddresses") or [] if e.get("value")],
            "phones": [t["value"] for t in p.get("phoneNumbers") or [] if t.get("value")],
            "organization": org, "kind": kind}


def contacts_search(args):
    query = str(args.get("query") or "").strip()
    if not query:
        raise Failure("Recherche vide")
    limit = max(1, min(30, int(args.get("limit") or 10)))
    rows, errors = [], []
    for account in chosen(args):
        try:
            # The People API wants a warm-up search before the first real one.
            api(account, "GET", f"{PEOPLE}/people:searchContacts", query={"query": "", "readMask": READ_MASK})
            got = api(account, "GET", f"{PEOPLE}/people:searchContacts",
                      query={"query": query, "readMask": READ_MASK, "pageSize": limit}).get("results", [])
            rows += [person(account, r["person"]) for r in got]
            # Colleagues in the company directory (Workspace accounts that granted it).
            if granted(account, DIRECTORY):
                try:
                    found = api(account, "GET", f"{PEOPLE}/people:searchDirectoryPeople", query={
                        "query": query, "readMask": READ_MASK, "pageSize": limit,
                        "sources": ["DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE", "DIRECTORY_SOURCE_TYPE_DOMAIN_CONTACT"]}).get("people", [])
                    seen = {e for r in rows for e in r["emails"]}
                    rows += [p for p in (person(account, f, "directory") for f in found) if not set(p["emails"]) & seen]
                except Failure:
                    pass  # a personal account has no directory
            other = api(account, "GET", f"{PEOPLE}/otherContacts:search",
                        query={"query": query, "readMask": "names,emailAddresses,phoneNumbers", "pageSize": limit}).get("results", [])
            seen = {e for r in rows for e in r["emails"]}
            rows += [p for p in (person(account, r["person"], "other") for r in other) if not set(p["emails"]) & seen]
        except Failure as e:
            errors.append(f"{account}: {e}")
    return {"contacts": rows[:limit], **({"errors": errors} if errors else {})}


# --- MCP ---------------------------------------------------------------------------

S, I, B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
OUT = {"type": "object", "required": ["to", "subject", "body"], "properties": {
    "to": S, "subject": S, "body": S, "reply_to_id": S, "account": S}}
ROLES = {
    "mail": {
        "accounts": (mail_accounts, "The Google accounts signed in.", {"type": "object", "properties": {}}),
        "list": (mail_list, "Newest Gmail messages of a label (INBOX by default).",
                 {"type": "object", "properties": {"account": S, "folder": S, "unread_only": B, "limit": I}}),
        "search": (mail_search, "Search Gmail (words, from:, subject:, since:, before:, is:unread).",
                   {"type": "object", "required": ["query"], "properties": {"query": S, "account": S, "limit": I}}),
        "read": (mail_read, "One message, as text, with attachment names.",
                 {"type": "object", "required": ["id"], "properties": {"id": S}}),
        "mark_read": (mail_mark_read, "Mark read (or unread with read: false).",
                      {"type": "object", "required": ["id"], "properties": {"id": S, "read": B}}),
        "draft": (mail_draft, "Save a Gmail draft (a reply with reply_to_id).", OUT),
        "send": (mail_send, "Send from Gmail (a reply with reply_to_id).", OUT),
    },
    "calendar": {
        "events": (calendar_events, "Google Calendar events from date for days, local times, Meet links.",
                   {"type": "object", "properties": {"date": S, "days": I, "account": S, "search": S}}),
        "calendars": (calendar_calendars, "Google calendars and whether they accept events.",
                      {"type": "object", "properties": {}}),
        "create": (calendar_create, "Add a Google Calendar event.",
                   {"type": "object", "required": ["title", "start"], "properties": {
                       "title": S, "start": S, "minutes": I, "location": S, "account": S, "calendar": S,
                       "attendees": {"type": "array", "items": S}, "meet": B}}),
        "busy": (calendar_busy, "Other people's busy periods (Google free/busy).",
                 {"type": "object", "required": ["emails"], "properties": {
                     "emails": {"type": "array", "items": S}, "date": S, "days": I, "account": S}}),
        "update": (calendar_update, "Change a Google Calendar event (one occurrence of a series).",
                   {"type": "object", "required": ["id", "event"], "properties": {
                       "id": S, "event": S, "title": S, "start": S, "minutes": I, "location": S}}),
        "delete": (calendar_delete, "Delete a Google Calendar event (one occurrence of a series).",
                   {"type": "object", "required": ["id", "event"], "properties": {"id": S, "event": S}}),
    },
    "contacts": {
        "search": (contacts_search, "Google contacts (and people you've emailed) by name or address.",
                   {"type": "object", "required": ["query"], "properties": {"query": S, "account": S, "limit": I}}),
    },
}
TOOLS = ROLES["mail"]


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Réponse de Google inattendue ou argument invalide"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out, "isError": False}


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None  # notification
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "samantha-google", "version": "0.2.0"}}
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
    global TOOLS
    role = sys.argv[1] if len(sys.argv) > 1 else "mail"
    TOOLS = ROLES.get(role, ROLES["mail"])
    if role == "calendar" and os.environ.get("SAMANTHA_SOCKET"):
        threading.Thread(target=remind_forever, daemon=True).start()
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
