#!/usr/bin/env python3
"""MCP stdio server of the `microsoft` extension (stdlib only).

A provider (Samantha's fronts serve it): Outlook mail and calendar of the user's
Microsoft accounts (Outlook.com, Hotmail, Microsoft 365) through Microsoft Graph.
Run as `server.py mail` or `server.py calendar`: the methods of that capability.
Access tokens come from Samantha's daemon (`oauth.token`, the user signed in with
`samantha account add microsoft`); this server never sees a password or refresh
token, and logs nothing.

Ids carry their account: `me@outlook.com/AAMkAG…` (an address has no `/`).
"""

import datetime
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = "2025-06-18"
GRAPH = "https://graph.microsoft.com/v1.0"
TIMEOUT = 20
MAX_LIST = 50
MAX_BODY = 20000
FRENCH = os.environ.get("SAMANTHA_LANGUAGE", os.environ.get("LANG", "fr")).startswith("fr")
SETUP = ("Aucun compte Microsoft: enregistre ton app (client_id dans [oauth.microsoft] de "
         "config.toml), puis `samantha account add microsoft`.")


class Failure(Exception):
    """A message for the model: the call failed, say why."""


# --- daemon and Graph --------------------------------------------------------------

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
                    raise Failure(SETUP if "no microsoft account" in err else f"Compte Microsoft: {err}")
                return reply["result"]
    raise Failure("Le démon a fermé la connexion")


def accounts():
    names = daemon("oauth.accounts", {"provider": "microsoft"})["accounts"]
    if not names:
        raise Failure(SETUP)
    return names


def graph(account, method, path, body=None, headers=None, query=None):
    token = daemon("oauth.token", {"provider": "microsoft", "account": account})["access_token"]
    url = GRAPH + path + (f"?{urllib.parse.urlencode(query, safe='$,')}" if query else "")
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json", **(headers or {})})
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
            raise Failure(f"Microsoft refuse l'accès ({e.code}): permission manquante ou consentement "
                          f"à renouveler (`samantha account add microsoft`). {msg}".strip())
        if e.code == 404:
            raise Failure("Introuvable chez Microsoft")
        raise Failure(f"Microsoft Graph a refusé la requête ({e.code}): {msg}".strip())
    except (urllib.error.URLError, TimeoutError, OSError):
        raise Failure("Microsoft injoignable (réseau ?)")
    except ValueError:
        raise Failure("Microsoft a répondu quelque chose d'illisible")


def chosen(args):
    """Accounts a call covers: the one named, else every account."""
    names = accounts()
    want = args.get("account")
    if want in (None, "", "all"):
        return names
    if want not in names:
        raise Failure(f"Pas de compte Microsoft {want!r}: {', '.join(names)}")
    return [want]


def split_id(value):
    account, _, native = str(value or "").partition("/")
    if not account or not native:
        raise Failure("id invalide: prends l'id renvoyé par la liste")
    return account, native


def local_zone():
    """IANA name of the local zone (Graph accepts it in Prefer: outlook.timezone)."""
    try:
        return os.path.realpath("/etc/localtime").split("zoneinfo/", 1)[1]
    except (IndexError, OSError):
        return "UTC"


def iso_local(utc):
    try:
        t = datetime.datetime.fromisoformat(str(utc).replace("Z", "+00:00"))
        return t.astimezone().isoformat(timespec="seconds")
    except ValueError:
        return str(utc)


def when(iso, now=None):
    """`date` for reading, in local time: "09:30" today, "hier 09:30", "2 oct."."""
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


# --- mail --------------------------------------------------------------------------

SELECT = "id,subject,from,receivedDateTime,isRead"


def row(account, m):
    sender = (m.get("from") or {}).get("emailAddress") or {}
    date = iso_local(m.get("receivedDateTime", ""))
    return {"id": f"{account}/{m['id']}", "account": account, "from": (sender.get("address") or "").lower(),
            "name": sender.get("name") or sender.get("address") or "", "subject": m.get("subject") or "",
            "date": date, "when": when(date), "unread": not m.get("isRead", True)}


def across(args, fetch):
    limit = max(1, min(MAX_LIST, int(args.get("limit") or 20)))
    rows, errors = [], []
    for account in chosen(args):
        try:
            rows += [row(account, m) for m in fetch(account, limit)]
        except Failure as e:
            errors.append(f"{account}: {e}")
    rows.sort(key=lambda r: r["date"], reverse=True)
    out = {"rows": rows[:limit]}
    if errors:
        out["errors"] = errors
    return out


def mail_accounts(args):
    names = accounts()
    return {"rows": [{"account": n, "label": "", "address": n, "default": i == 0} for i, n in enumerate(names)]}


def mail_list(args):
    folder = str(args.get("folder") or "inbox")
    if not folder.replace("_", "").isalnum():
        raise Failure("Dossier invalide (inbox, sentitems, drafts, archive…)")

    def fetch(account, limit):
        q = {"$top": limit, "$orderby": "receivedDateTime desc", "$select": SELECT}
        if args.get("unread_only"):
            q["$filter"] = "isRead eq false"
        return graph(account, "GET", f"/me/mailFolders/{folder}/messages", query=q).get("value", [])
    return across(args, fetch)


def kql(query):
    """The mail capability's query syntax in Graph's KQL (since:/before:/is:unread)."""
    out = []
    for word in str(query).split():
        if word.startswith("since:"):
            out.append(f"received>={word[6:]}")
        elif word.startswith("before:"):
            out.append(f"received<={word[7:]}")
        elif word == "is:unread":
            out.append("isread:false")
        else:
            out.append(word)
    return " ".join(out)


def mail_search(args):
    query = str(args.get("query") or "").strip()
    if not query:
        raise Failure("Recherche vide")

    def fetch(account, limit):
        q = {"$search": f'"{kql(query).replace(chr(34), "")}"', "$top": limit, "$select": SELECT}
        return graph(account, "GET", "/me/messages", query=q).get("value", [])
    return across(args, fetch)


def mail_read(args):
    account, native = split_id(args.get("id"))
    m = graph(account, "GET", f"/me/messages/{urllib.parse.quote(native, safe='')}",
              headers={"Prefer": 'outlook.body-content-type="text"'},
              query={"$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,body,isRead,hasAttachments"})
    body = (m.get("body") or {}).get("content") or ""
    out = row(account, m)
    addrs = lambda k: ", ".join(r["emailAddress"].get("address", "") for r in m.get(k) or [])
    out.update(to=addrs("toRecipients"), cc=addrs("ccRecipients"), body=body[:MAX_BODY],
               truncated=len(body) > MAX_BODY, attachments=[])
    if m.get("hasAttachments"):
        att = graph(account, "GET", f"/me/messages/{urllib.parse.quote(native, safe='')}/attachments",
                    query={"$select": "name,size"})
        out["attachments"] = [{"name": a.get("name"), "size": a.get("size")} for a in att.get("value", [])]
    return out


def mail_mark_read(args):
    account, native = split_id(args.get("id"))
    graph(account, "PATCH", f"/me/messages/{urllib.parse.quote(native, safe='')}",
          {"isRead": args.get("read", True) is not False})
    return {"id": f"{account}/{native}", "read": args.get("read", True) is not False}


def recipients(to):
    out = []
    for part in str(to or "").split(","):
        part = part.strip()
        if not part:
            continue
        name, _, addr = part.rpartition("<")
        addr = addr.rstrip(">").strip() if addr != part else part
        if "@" not in addr:
            raise Failure(f"Destinataire invalide: {part}")
        out.append({"emailAddress": {"address": addr, "name": name.strip().strip('"')}})
    if not out:
        raise Failure("Aucun destinataire")
    return out


def outgoing(args, send):
    subject, body = str(args.get("subject") or ""), str(args.get("body") or "")
    if args.get("reply_to_id"):
        account, native = split_id(args["reply_to_id"])
        path = f"/me/messages/{urllib.parse.quote(native, safe='')}"
        if send:
            graph(account, "POST", f"{path}/reply", {"comment": body})
            return {"sent": True, "account": account}
        draft = graph(account, "POST", f"{path}/createReply", {"comment": body})
        return {"draft": True, "id": f"{account}/{draft.get('id', '')}", "account": account}
    names = accounts()
    account = args.get("account") or names[0]
    if account not in names:
        raise Failure(f"Pas de compte Microsoft {account!r}: {', '.join(names)}")
    message = {"subject": subject, "body": {"contentType": "Text", "content": body},
               "toRecipients": recipients(args.get("to"))}
    if send:
        graph(account, "POST", "/me/sendMail", {"message": message, "saveToSentItems": True})
        return {"sent": True, "account": account}
    draft = graph(account, "POST", "/me/messages", message)
    return {"draft": True, "id": f"{account}/{draft.get('id', '')}", "account": account}


def mail_draft(args):
    return outgoing(args, send=False)


def mail_send(args):
    return outgoing(args, send=True)


# --- calendar ----------------------------------------------------------------------

def day_name(t):
    today = datetime.datetime.now().astimezone().date()
    delta = (t.date() - today).days
    names = {0: "aujourd'hui", 1: "demain", -1: "hier"} if FRENCH else {0: "today", 1: "tomorrow", -1: "yesterday"}
    if delta in names:
        return names[delta]
    days = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"] if FRENCH else \
        ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    return f"{days[t.weekday()]} {t.day}"


# Video meeting links typed into events (Graph gives Teams' own): (pattern, service).
MEETINGS = [(r"zoom\.us/(?:j|my|w)/", "Zoom"), (r"meet\.google\.com/[a-z]{3}-", "Google Meet"),
            (r"teams\.(?:microsoft|live)\.com/", "Teams"), (r"webex\.com/", "Webex"),
            (r"meet\.jit\.si/", "Jitsi"), (r"whereby\.com/", "Whereby"), (r"kmeet\.infomaniak\.com/", "kMeet")]


def meeting(text):
    for url in re.findall(r"https://[^\s<>\"']+", text or ""):
        for pattern, service in MEETINGS:
            if re.search(pattern, url):
                return {"meeting_url": url.rstrip(").,;"), "meeting": service}
    return {"meeting_url": "", "meeting": ""}


def calendar_events(args):
    days = max(1, min(62, int(args.get("days") or 1)))
    try:
        first = (datetime.datetime.strptime(str(args["date"]), "%Y-%m-%d") if args.get("date")
                 else datetime.datetime.now()).replace(hour=0, minute=0, second=0, microsecond=0)
    except ValueError:
        raise Failure("date: AAAA-MM-JJ")
    last = first + datetime.timedelta(days=days)
    q = {"startDateTime": first.isoformat(), "endDateTime": last.isoformat(),
         "$orderby": "start/dateTime", "$top": 100,
         "$select": "subject,start,end,isAllDay,location,bodyPreview,isCancelled,onlineMeeting,onlineMeetingProvider"}
    search = str(args.get("search") or "").lower()
    events, errors = [], []
    for account in chosen(args):
        try:
            got = graph(account, "GET", "/me/calendarView", query=q,
                        headers={"Prefer": f'outlook.timezone="{local_zone()}"'}).get("value", [])
        except Failure as e:
            errors.append(f"{account}: {e}")
            continue
        for e in got:
            if e.get("isCancelled"):
                continue
            start = datetime.datetime.fromisoformat(e["start"]["dateTime"][:19])
            end = datetime.datetime.fromisoformat(e["end"]["dateTime"][:19])
            ev = {"title": e.get("subject") or "", "calendar": "Outlook", "account": account,
                  "all_day": bool(e.get("isAllDay")),
                  "start": start.strftime("%Y-%m-%d") if e.get("isAllDay") else start.strftime("%Y-%m-%d %H:%M"),
                  "end": end.strftime("%Y-%m-%d %H:%M"), "day": day_name(start),
                  "location": (e.get("location") or {}).get("displayName") or "",
                  "description": (e.get("bodyPreview") or "")[:500]}
            join = (e.get("onlineMeeting") or {}).get("joinUrl")
            if join:
                provider = e.get("onlineMeetingProvider") or ""
                ev.update(meeting_url=join, meeting="Teams" if "teams" in provider.lower() else provider or "Teams")
            else:
                ev.update(meeting(f"{ev['location']} {e.get('bodyPreview') or ''}"))
            if not search or search in f"{ev['title']} {ev['location']} {ev['description']}".lower():
                events.append(ev)
    events.sort(key=lambda e: e["start"])
    out = {"events": events}
    if errors:
        out["errors"] = errors
    return out


def calendar_calendars(args):
    rows, errors = [], []
    for account in chosen(args):
        try:
            for c in graph(account, "GET", "/me/calendars", query={"$select": "id,name,canEdit"}).get("value", []):
                rows.append({"account": account, "calendar": c.get("name", ""), "writable": bool(c.get("canEdit"))})
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
        raise Failure(f"Pas de compte Microsoft {account!r}: {', '.join(names)}")
    zone = local_zone()
    path = "/me/events"
    if args.get("calendar"):
        cals = graph(account, "GET", "/me/calendars", query={"$select": "id,name,canEdit"}).get("value", [])
        match = [c for c in cals if str(args["calendar"]).lower() in c.get("name", "").lower() and c.get("canEdit")]
        if not match:
            raise Failure("Calendrier introuvable ou en lecture seule")
        path = f"/me/calendars/{urllib.parse.quote(match[0]['id'], safe='')}/events"
    event = {"subject": title,
             "start": {"dateTime": start.isoformat(), "timeZone": zone},
             "end": {"dateTime": (start + datetime.timedelta(minutes=minutes)).isoformat(), "timeZone": zone}}
    if args.get("location"):
        event["location"] = {"displayName": str(args["location"])}
    graph(account, "POST", path, event)
    return {"created": title, "account": account, "start": start.strftime("%Y-%m-%d %H:%M"), "minutes": minutes}


# --- MCP ---------------------------------------------------------------------------

S, I, B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
OUT = {"type": "object", "required": ["to", "subject", "body"], "properties": {
    "to": S, "subject": S, "body": S, "reply_to_id": S, "account": S}}
MAIL = {
    "accounts": (mail_accounts, "The Microsoft accounts signed in.", {"type": "object", "properties": {}}),
    "list": (mail_list, "Newest messages of a folder (inbox by default), every account or one.",
             {"type": "object", "properties": {"account": S, "folder": S, "unread_only": B, "limit": I}}),
    "search": (mail_search, "Search mail (words, from:, subject:, since:, before:, is:unread).",
               {"type": "object", "required": ["query"], "properties": {"query": S, "account": S, "limit": I}}),
    "read": (mail_read, "One message, as text, with attachment names.",
             {"type": "object", "required": ["id"], "properties": {"id": S}}),
    "mark_read": (mail_mark_read, "Mark read (or unread with read: false).",
                  {"type": "object", "required": ["id"], "properties": {"id": S, "read": B}}),
    "draft": (mail_draft, "Save a draft (a reply with reply_to_id).", OUT),
    "send": (mail_send, "Send a mail (a reply with reply_to_id).", OUT),
}
CALENDAR = {
    "events": (calendar_events, "Outlook events from date for days, local times.",
               {"type": "object", "properties": {"date": S, "days": I, "account": S, "search": S}}),
    "calendars": (calendar_calendars, "Outlook calendars and whether they accept events.",
                  {"type": "object", "properties": {}}),
    "create": (calendar_create, "Add an Outlook event.",
               {"type": "object", "required": ["title", "start"], "properties": {
                   "title": S, "start": S, "minutes": I, "location": S, "account": S, "calendar": S}}),
}
TOOLS = MAIL  # main() picks the role's set


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Réponse de Microsoft inattendue ou "
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
                  "serverInfo": {"name": "samantha-microsoft", "version": "0.1.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": n, "description": d, "inputSchema": s} for n, (_, d, s) in TOOLS.items()]}
    elif method == "tools/call" and params.get("name") in TOOLS:
        result = call(params["name"], params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": "method not found"}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main():
    global TOOLS
    TOOLS = CALENDAR if sys.argv[1:2] == ["calendar"] else MAIL
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
