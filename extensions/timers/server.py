#!/usr/bin/env python3
"""MCP stdio server of the `timers` extension (stdlib only).

Timers ("10 minutes", "pomodoro") and alarms ("à 7 h 30"), kept in
~/.local/state/samantha/timers/timers.json so a restart loses none. Each running one
is an island activity (`timers.activity`); at its end the island says so, the
freedesktop alarm sound plays (pw-play) and a `timers.done` event goes out for
watchers. One that ended while the server was down is announced at start if it ended
less than an hour ago. Nothing is logged.
"""

import datetime
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid

PROTOCOL = "2025-06-18"
STATE = os.path.expanduser("~/.local/state/samantha/timers/timers.json")
SOUND = "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga"
MAX_SECONDS = 7 * 24 * 3600
LATE_ANNOUNCE = 3600
FRENCH = os.environ.get("SAMANTHA_LANGUAGE", os.environ.get("LANG", "fr")).startswith("fr")

now = time.time  # replaced in tests
_lock = threading.Lock()
_timers = []      # [{id, label, kind, ends}]


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def words(fr, en):
    return fr if FRENCH else en


def hhmm(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M")


def save():
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".new"
    with open(tmp, "w") as f:
        json.dump(_timers, f)
    os.replace(tmp, STATE)


def load():
    try:
        with open(STATE) as f:
            data = json.load(f)
        _timers[:] = [t for t in data if isinstance(t, dict) and {"id", "label", "kind", "ends"} <= t.keys()]
    except (OSError, ValueError):
        _timers[:] = []


def daemon(method, params, timeout=10):
    """One request on the daemon socket under the `extension` role (best effort)."""
    path, token = os.environ.get("SAMANTHA_SOCKET"), os.environ.get("SAMANTHA_EXTENSION_TOKEN")
    if not path or not token:
        return None
    try:
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
                    return reply
    except (OSError, ValueError):
        return None


def publish(kind, data):
    daemon("events.publish", {"type": kind, "data": data})


def show(t):
    name = t["label"] or words("Minuteur" if t["kind"] == "timer" else "Alarme",
                               "Timer" if t["kind"] == "timer" else "Alarm")
    publish("timers.activity", {"key": t["id"], "text": name,
                                "sub": words("fin à ", "ends at ") + hhmm(t["ends"])})


def ring(t, late=False):
    name = t["label"] or words("Minuteur" if t["kind"] == "timer" else "Alarme",
                               "Timer" if t["kind"] == "timer" else "Alarm")
    text = f"{name} " + (words("terminé pendant l'absence", "ended while away") if late
                         else words("terminé", "done"))
    publish("timers.activity", {"key": t["id"], "text": text, "icon": "check", "ttl_s": 30})
    publish("timers.done", {"id": t["id"], "label": t["label"], "kind": t["kind"], "late": late})
    if not late and os.path.exists(SOUND):
        for _ in range(2):
            try:
                subprocess.run(["pw-play", SOUND], timeout=10, capture_output=True)
            except (OSError, subprocess.TimeoutExpired):
                break


def due():
    """Removes and returns the timers that have ended."""
    with _lock:
        t0 = now()
        ended = [t for t in _timers if t["ends"] <= t0]
        if ended:
            _timers[:] = [t for t in _timers if t["ends"] > t0]
            save()
    return ended


def tick_forever():
    while True:
        for t in due():
            ring(t)
        time.sleep(1)


def start_up():
    """Loads the state, announces what ended while down, shows what runs."""
    load()
    t0 = now()
    for t in due():
        if t0 - t["ends"] < LATE_ANNOUNCE:
            ring(t, late=True)
    for t in list(_timers):
        show(t)


def parse_at(at):
    """"7:30", "07h30", "7h", "19" → the next such time."""
    m = re.match(r"^\s*(\d{1,2})\s*(?:[:h]\s*(\d{2})?)?\s*$", str(at))
    h, mi = (int(m.group(1)), int(m.group(2) or 0)) if m else (-1, -1)
    if not (0 <= h < 24 and 0 <= mi < 60):
        raise Failure(words("Heure invalide: attendu HH:MM", "Invalid time: expected HH:MM"))
    t = datetime.datetime.fromtimestamp(now()).replace(hour=h, minute=mi, second=0, microsecond=0)
    if t.timestamp() <= now():
        t += datetime.timedelta(days=1)
    return t.timestamp()


def start(args):
    label = str(args.get("label") or "").strip()[:60]
    if args.get("at"):
        ends, kind = parse_at(args["at"]), "alarm"
    else:
        try:
            seconds = float(args.get("minutes") or 0) * 60 + float(args.get("seconds") or 0)
        except (TypeError, ValueError):
            raise Failure(words("Durée invalide", "Invalid duration"))
        if not 0 < seconds <= MAX_SECONDS:
            raise Failure(words("Durée invalide (entre 1 seconde et 7 jours)",
                                "Invalid duration (1 second to 7 days)"))
        ends, kind = now() + seconds, "timer"
    t = {"id": uuid.uuid4().hex[:8], "label": label, "kind": kind, "ends": ends}
    with _lock:
        _timers.append(t)
        save()
    show(t)
    return {"id": t["id"], "kind": kind, "label": label, "ends": hhmm(ends),
            "remaining_s": round(ends - now())}


def listing(args):
    with _lock:
        rows = sorted(_timers, key=lambda t: t["ends"])
    return {"timers": [{"id": t["id"], "kind": t["kind"], "label": t["label"],
                        "ends": hhmm(t["ends"]), "remaining_s": max(0, round(t["ends"] - now()))}
                       for t in rows]}


def cancel(args):
    which = str(args.get("id") or "").strip()
    with _lock:
        gone = [t for t in _timers if which in ("all", t["id"]) or
                (which and which.lower() == t["label"].lower())]
        if not gone:
            raise Failure(words("Aucun minuteur ne correspond", "No timer matches"))
        _timers[:] = [t for t in _timers if t not in gone]
        save()
    for t in gone:
        publish("timers.activity", {"key": t["id"]})
    return {"cancelled": [t["id"] for t in gone]}


# --- MCP ---------------------------------------------------------------------------

S, N = {"type": "string"}, {"type": "number"}
ROW = {"type": "object", "properties": {"id": S, "kind": S, "label": S, "ends": S,
                                        "remaining_s": {"type": "integer"}}}
TOOLS = {
    "start": (start, {
        "description": "Start a timer (minutes and/or seconds) or an alarm (at, HH:MM, the "
                       "next such time), with an optional label. It shows on the island "
                       "and rings at the end.",
        "inputSchema": {"type": "object", "properties": {
            "minutes": N, "seconds": N, "at": S, "label": S}},
        "outputSchema": ROW,
    }),
    "list": (listing, {
        "description": "Running timers and alarms, soonest first, with time remaining.",
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {"type": "object", "properties": {"timers": {"type": "array", "items": ROW}}},
    }),
    "cancel": (cancel, {
        "description": "Cancel a timer or alarm by id or label, or all of them (id: \"all\").",
        "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": S}},
    }),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except OSError:
        return {"content": [{"type": "text", "text": words(
            "Impossible d'enregistrer les minuteurs", "Can't save the timers")}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out, "isError": False}


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None  # notification
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "samantha-timers", "version": "0.1.0"}}
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
    start_up()
    threading.Thread(target=tick_forever, daemon=True).start()
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
