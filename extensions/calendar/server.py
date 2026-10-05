#!/usr/bin/env python3
"""MCP stdio server of the `calendar` front (stdlib only).

The front's own intelligence over its capability: `free` reads `calendar.events` from
every provider through Samantha's daemon (`capability.call`, read methods of its own
capability only) and gives the free slots between them. No network, no files.
"""

import datetime
import json
import os
import re
import socket
import sys

PROTOCOL = "2025-06-18"
FRENCH = os.environ.get("SAMANTHA_LANGUAGE", os.environ.get("LANG", "fr")).startswith("fr")


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def daemon(method, params, timeout=50):
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
                    raise Failure(f"Agenda: {reply.get('error')}")
                return reply["result"]
    raise Failure("Le démon a fermé la connexion")


def now():
    return datetime.datetime.now()


def day_name(d, today):
    delta = (d - today).days
    names = {0: "aujourd'hui", 1: "demain"} if FRENCH else {0: "today", 1: "tomorrow"}
    if delta in names:
        return names[delta]
    days = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"] if FRENCH else \
        ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    return f"{days[d.weekday()]} {d.day}"


def hour(value, default):
    try:
        return datetime.datetime.strptime(str(value or default), "%H:%M").time()
    except ValueError:
        raise Failure("from et to: HH:MM")


EMAIL = re.compile(r"[^@\s,<>]+@[^@\s,<>]+\.[^@\s,<>]+")


def others_busy(people, first, days):
    """Busy periods of `people` (calendar.busy from every provider), whose calendars
    weren't readable, and errors."""
    try:
        got = daemon("capability.call", {"method": "busy",
                                         "args": {"emails": people, "date": first.isoformat(), "days": days}})
    except Failure as e:
        return [], people, [str(e)]
    rows = got.get("busy", [])
    seen = {str(r.get("email", "")).lower() for r in rows if r.get("seen")}
    busy = [(datetime.datetime.strptime(r["start"], "%Y-%m-%d %H:%M"), datetime.datetime.strptime(r["end"], "%Y-%m-%d %H:%M"))
            for r in rows if r.get("start")]
    return busy, [e for e in people if e.lower() not in seen], got.get("errors", [])


def free(args):
    days = max(1, min(14, int(args.get("days") or 1)))
    t0 = now()
    try:
        first = datetime.date.fromisoformat(str(args["date"])) if args.get("date") else t0.date()
    except ValueError:
        raise Failure("date: AAAA-MM-JJ")
    opens, closes = hour(args.get("from"), "09:00"), hour(args.get("to"), "18:00")
    if closes <= opens:
        raise Failure("to doit être après from")
    minutes = max(15, min(8 * 60, int(args.get("minutes") or 30)))
    people = args.get("with") or []
    people = [str(e).strip() for e in (people.split(",") if isinstance(people, str) else people) if str(e).strip()]
    bad = [e for e in people if not EMAIL.fullmatch(e)]
    if bad:
        raise Failure(f"with: des adresses e-mail ({', '.join(bad)}): cherche-les avec contacts.search")
    got = daemon("capability.call", {"method": "events", "args": {"date": first.isoformat(), "days": days}})
    # ponytail: all-day events (birthdays, holidays) don't block time, "out of office"
    # included; read their busy/free status from providers if that matters.
    busy = sorted((datetime.datetime.strptime(e["start"], "%Y-%m-%d %H:%M"),
                   datetime.datetime.strptime(e["end"], "%Y-%m-%d %H:%M"))
                  for e in got.get("events", []) if not e.get("all_day"))
    errors, unseen = list(got.get("errors") or []), []
    if people:
        theirs, unseen, more = others_busy(people, first, days)
        busy = sorted(busy + theirs)
        errors += more
    slots = []
    for n in range(days):
        d = first + datetime.timedelta(days=n)
        cursor, end = datetime.datetime.combine(d, opens), datetime.datetime.combine(d, closes)
        if cursor < t0:
            # From now, at the next 5 minutes.
            cursor = t0.replace(second=0, microsecond=0) + datetime.timedelta(minutes=-t0.minute % 5 or 5)
        for b_start, b_end in busy + [(end, end)]:
            if b_end <= cursor:
                continue
            gap_end = min(b_start, end)
            if (gap_end - cursor).total_seconds() >= minutes * 60:
                slots.append({"date": d.isoformat(), "day": day_name(d, t0.date()),
                              "start": cursor.strftime("%H:%M"), "end": gap_end.strftime("%H:%M"),
                              "minutes": int((gap_end - cursor).total_seconds() // 60)})
            cursor = max(cursor, b_end)
            if cursor >= end:
                break
    return {"slots": slots, **({"unseen": unseen} if unseen else {}), **({"errors": errors} if errors else {})}


S, I = {"type": "string"}, {"type": "integer"}
TOOLS = {
    "free": (free, {
        "description": "Free slots across every calendar: `days` from `date` (default today, 1 day; up to 14), "
                       "between `from` and `to` each day (HH:MM, default 09:00 and 18:00), at least `minutes` long "
                       "(default 30). Today starts now. All-day events don't block time. `with`: other people's "
                       "addresses, their busy times count too (colleagues whose calendar the user's Google or "
                       "Microsoft account can see); `unseen` lists those whose calendar couldn't be read: the slots "
                       "don't account for them. {slots: [{date, day, start, end, minutes}], unseen?, errors?}",
        "inputSchema": {"type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD"}, "days": I,
            "from": {"type": "string", "description": "HH:MM"}, "to": {"type": "string", "description": "HH:MM"},
            "minutes": I, "with": {"type": "array", "items": S}}},
        "outputSchema": {"type": "object", "properties": {"slots": {"type": "array", "items": {
            "type": "object", "properties": {"date": S, "day": S, "start": S, "end": S, "minutes": I}}},
            "unseen": {"type": "array", "items": S}, "errors": {"type": "array", "items": S}}},
    }),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, TypeError, ValueError):
        return {"content": [{"type": "text", "text": "Argument invalide ou agenda illisible"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out, "isError": False}


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "samantha-calendar", "version": "0.4.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": n, **spec} for n, (_, spec) in TOOLS.items()]}
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
