#!/usr/bin/env python3
"""MCP stdio server of the `clipboard` extension (stdlib only).

Clipboard history: `wl-paste --watch` runs `server.py record` at each copy, which
appends the text to ~/.local/state/samantha/clipboard/history.jsonl (owner-only,
200 entries, 3 days). What a password manager copies (it marks it
`x-kde-passwordManagerHint`) is never kept, nor anything that isn't text. Tools read
the history and put an entry back on the clipboard (`wl-copy`). Nothing is logged.
"""

import json
import os
import subprocess
import sys
import threading
import time

PROTOCOL = "2025-06-18"
DIR = os.path.expanduser("~/.local/state/samantha/clipboard")
HISTORY = os.path.join(DIR, "history.jsonl")
KEEP = 200
KEEP_DAYS = 3
MAX_TEXT = 5000
SECRET_HINT = "x-kde-passwordManagerHint"

now = time.time  # replaced in tests


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def load():
    cutoff = now() - KEEP_DAYS * 86400
    items = []
    try:
        with open(HISTORY) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("time", 0) >= cutoff and isinstance(e.get("text"), str):
                    items.append(e)
    except OSError:
        pass
    return items[-KEEP:]


def save(items):
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    tmp = HISTORY + ".new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        for e in items:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    os.replace(tmp, HISTORY)


def keep(text, types):
    """One copy, unless it's a secret, empty, or the same as the last one."""
    if SECRET_HINT in types or not text.strip():
        return False
    items = load()
    if items and items[-1]["text"] == text[:MAX_TEXT]:
        return False
    items.append({"time": now(), "text": text[:MAX_TEXT], "truncated": len(text) > MAX_TEXT})
    save(items[-KEEP:])
    return True


def record():
    """`wl-paste --watch server.py record`: stdin is what was just copied."""
    try:
        types = subprocess.run(["wl-paste", "--list-types"], capture_output=True, text=True, timeout=5).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        types = []
    text = sys.stdin.read()
    keep(text, types)


def watch_forever():
    while True:
        try:
            subprocess.run(["wl-paste", "--type", "text", "--watch", sys.executable, os.path.abspath(__file__), "record"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
        time.sleep(10)  # wl-paste ended (display gone): watch again


def row(i, e):
    return {"index": i, "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(e["time"])),
            "text": e["text"][:300], "chars": len(e["text"]), "truncated": e.get("truncated", False) or len(e["text"]) > 300}


def history(args):
    items = list(enumerate(reversed(load())))  # 0 = the newest
    q = str(args.get("search") or "").lower()
    if q:
        items = [(i, e) for i, e in items if q in e["text"].lower()]
    limit = max(1, min(50, int(args.get("limit") or 10)))
    return {"entries": [row(i, e) for i, e in items[:limit]]}


def get(args):
    items = list(reversed(load()))
    try:
        e = items[int(args.get("index", 0))]
    except (ValueError, IndexError):
        raise Failure("Index hors de l'historique (0 = la dernière copie)")
    return {"index": int(args.get("index", 0)), "text": e["text"], "truncated": e.get("truncated", False)}


def copy(args):
    if args.get("text") is not None:
        text = str(args["text"])
    else:
        text = get(args)["text"]
    if not text:
        raise Failure("Rien à copier")
    try:
        subprocess.run(["wl-copy"], input=text, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        raise Failure("Impossible d'écrire dans le presse-papiers (wl-copy)")
    return {"copied": len(text)}


def clear(args):
    count = len(load())
    save([])
    return {"cleared": count}


S, I = {"type": "string"}, {"type": "integer"}
TOOLS = {
    "history": (history, "What the user copied (last 3 days), newest first (index 0); optional search.",
                {"type": "object", "properties": {"search": S, "limit": I}}),
    "get": (get, "One entry in full, by index (0 = the last copy).",
            {"type": "object", "properties": {"index": I}}),
    "copy": (copy, "Put text, or a history entry (index), on the clipboard.",
             {"type": "object", "properties": {"text": S, "index": I}}),
    "clear": (clear, "Erase the clipboard history.", {"type": "object", "properties": {}}),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, TypeError, ValueError, AttributeError, OSError):
        return {"content": [{"type": "text", "text": "Argument invalide ou historique illisible"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out, "isError": False}


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None  # notification
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "samantha-clipboard", "version": "0.1.0"}}
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
    if sys.argv[1:2] == ["record"]:
        return record()
    threading.Thread(target=watch_forever, daemon=True).start()
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
