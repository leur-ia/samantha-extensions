#!/usr/bin/env python3
"""MCP stdio server of the `notifications` extension (stdlib only).

Keeps the desktop notifications the user was sent, readable again: it watches the
session bus for `org.freedesktop.Notifications.Notify` calls (`busctl --user
monitor`; whatever daemon shows them keeps doing so) and appends each to
~/.local/state/samantha/notifications/history.jsonl, owner-only, for 7 days at most.
Bodies may hold codes and messages: personal data, never logged.
"""

import json
import os
import subprocess
import sys
import threading
import time

PROTOCOL = "2025-06-18"
DIR = os.path.expanduser("~/.local/state/samantha/notifications")
HISTORY = os.path.join(DIR, "history.jsonl")
SEEN = os.path.join(DIR, "seen")
KEEP_DAYS = 7
MAX_KEPT = 2000
MAX_BODY = 500
MONITOR = ["busctl", "--user", "monitor", "--json=short", "--match",
           "type=method_call,interface=org.freedesktop.Notifications,member=Notify"]
URGENCY = {0: "low", 1: "normal", 2: "critical"}

now = time.time  # replaced in tests
_lock = threading.Lock()
_items = []      # oldest first


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def parse(line):
    """A notification from one `busctl monitor` JSON line, or None."""
    try:
        msg = json.loads(line)
        app, _replaces, _icon, summary, body, _actions, hints, _timeout = msg["payload"]["data"]
    except (ValueError, KeyError, TypeError):
        return None
    urgency = hints.get("urgency", {}) if isinstance(hints, dict) else {}
    urgency = urgency.get("data") if isinstance(urgency, dict) else urgency
    stamp = msg.get("timestamp-realtime")
    return {"time": stamp / 1e6 if isinstance(stamp, (int, float)) else now(),
            "app": str(app)[:80], "summary": str(summary)[:200],
            "body": str(body)[:MAX_BODY], "urgency": URGENCY.get(urgency, "normal")}


def load():
    cutoff = now() - KEEP_DAYS * 86400
    items = []
    try:
        with open(HISTORY) as f:
            for line in f:
                try:
                    n = json.loads(line)
                except ValueError:
                    continue
                if isinstance(n, dict) and n.get("time", 0) >= cutoff:
                    items.append(n)
    except OSError:
        pass
    _items[:] = items[-MAX_KEPT:]
    rewrite()


def rewrite():
    """The file holds what's kept (old entries dropped at start)."""
    try:
        os.makedirs(DIR, mode=0o700, exist_ok=True)
        tmp = HISTORY + ".new"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            for n in _items:
                f.write(json.dumps(n, ensure_ascii=False) + "\n")
        os.replace(tmp, HISTORY)
    except OSError:
        pass


def add(n):
    with _lock:
        _items.append(n)
        del _items[:-MAX_KEPT]
        try:
            fd = os.open(HISTORY, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(json.dumps(n, ensure_ascii=False) + "\n")
        except OSError:
            pass


def monitor_forever():
    while True:
        try:
            proc = subprocess.Popen(MONITOR, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    stdin=subprocess.DEVNULL, text=True)
            for line in proc.stdout:
                n = parse(line)
                if n:
                    add(n)
        except OSError:
            pass
        time.sleep(10)  # busctl ended (bus restart): watch again


def last_seen():
    try:
        with open(SEEN) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return 0.0


def mark_seen(t):
    try:
        with open(SEEN, "w") as f:
            f.write(str(t))
    except OSError:
        pass


def row(n):
    return {"time": time.strftime("%Y-%m-%d %H:%M", time.localtime(n["time"])),
            "app": n["app"], "summary": n["summary"], "body": n["body"], "urgency": n["urgency"]}


def recent(args):
    limit = max(1, min(100, int(args.get("limit") or 30)))
    app = str(args.get("app") or "").lower()
    since = now() - float(args["minutes"]) * 60 if args.get("minutes") else 0
    new_only = bool(args.get("new_only"))
    if new_only:
        since = max(since, last_seen())
    with _lock:
        rows = [n for n in _items if n["time"] > since and (not app or app in n["app"].lower())]
    if new_only:
        mark_seen(now())
    return {"count": len(rows), "notifications": [row(n) for n in rows[-limit:]][::-1]}


def search(args):
    q = str(args.get("query") or "").strip().lower()
    if not q:
        raise Failure("Recherche vide")
    with _lock:
        rows = [n for n in _items if q in f"{n['app']} {n['summary']} {n['body']}".lower()]
    limit = max(1, min(50, int(args.get("limit") or 20)))
    return {"count": len(rows), "notifications": [row(n) for n in rows[-limit:]][::-1]}


def apps(args):
    counts = {}
    with _lock:
        for n in _items:
            counts[n["app"]] = counts.get(n["app"], 0) + 1
    return {"apps": [{"app": a, "count": c} for a, c in sorted(counts.items(), key=lambda x: -x[1])]}


def clear(args):
    with _lock:
        count = len(_items)
        _items.clear()
        rewrite()
    return {"cleared": count}


# --- MCP ---------------------------------------------------------------------------

S, I = {"type": "string"}, {"type": "integer"}
ROWS = {"type": "object", "properties": {"count": I, "notifications": {"type": "array", "items": {
    "type": "object", "properties": {"time": S, "app": S, "summary": S, "body": S, "urgency": S}}}}}
TOOLS = {
    "recent": (recent, {
        "description": "Notifications received, newest first: over the last `minutes`, from one "
                       "app (name fragment), or only those since the last new_only check (what "
                       "the user missed). Kept 7 days.",
        "inputSchema": {"type": "object", "properties": {
            "minutes": {"type": "number"}, "app": S, "new_only": {"type": "boolean"}, "limit": I}},
        "outputSchema": ROWS,
    }),
    "search": (search, {
        "description": "Notifications whose app, title or text contains the query.",
        "inputSchema": {"type": "object", "required": ["query"], "properties": {"query": S, "limit": I}},
        "outputSchema": ROWS,
    }),
    "apps": (apps, {
        "description": "Which apps sent notifications, and how many (last 7 days).",
        "inputSchema": {"type": "object", "properties": {}},
    }),
    "clear": (clear, {
        "description": "Erase the whole notification history.",
        "inputSchema": {"type": "object", "properties": {}},
    }),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Argument invalide"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out, "isError": False}


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None  # notification
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "samantha-notifications", "version": "0.1.0"}}
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
    load()
    threading.Thread(target=monitor_forever, daemon=True).start()
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
