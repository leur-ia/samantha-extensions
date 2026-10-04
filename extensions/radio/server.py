#!/usr/bin/env python3
"""MCP stdio server of the `radio` extension (stdlib only).

Live radio from the community radio-browser.info catalog (free, no account), played
with `ffplay` (FFmpeg) as a child of this server through PipeWire: one station at a
time, stopped with `stop` or when the server ends. While it plays, an island activity
(`radio.activity`) names the station. Failures are `isError` results, never a crash;
nothing is logged.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = "2025-06-18"
MIRRORS = ["https://all.api.radio-browser.info", "https://de1.api.radio-browser.info",
           "https://fi1.api.radio-browser.info"]
TIMEOUT = 8
AGENT = "Samantha/0.1 (radio extension)"
PLAYER = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]

_lock = threading.Lock()
_playing = {"proc": None, "station": None}


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def api(path, params=None):
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    last = None
    for base in MIRRORS:
        req = urllib.request.Request(f"{base}/json/{path}{query}", headers={"User-Agent": AGENT})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise Failure(f"radio-browser a refusé la requête ({e.code})")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e  # next mirror
        except ValueError:
            raise Failure("radio-browser a répondu quelque chose d'illisible")
    raise Failure(f"Annuaire des radios injoignable (réseau ?): {getattr(last, 'reason', last)}")


def station(s):
    return {"id": s.get("stationuuid", ""), "name": (s.get("name") or "").strip(),
            "country": s.get("countrycode", ""), "tags": s.get("tags", "")[:80],
            "codec": s.get("codec", ""), "bitrate": s.get("bitrate", 0),
            "url": s.get("url_resolved") or s.get("url", "")}


def search(args):
    params = {"limit": max(1, min(20, int(args.get("limit") or 8))), "hidebroken": "true",
              "order": "clickcount", "reverse": "true"}
    if args.get("query"):
        params["name"] = str(args["query"]).strip()
    if args.get("country"):
        params["countrycode"] = str(args["country"]).strip().upper()[:2]
    if args.get("tag"):
        params["tag"] = str(args["tag"]).strip().lower()
    if len(params) == 4:
        raise Failure("Donne un nom de radio, un pays (code FR, BE…) ou un genre (jazz, news…)")
    return {"stations": [station(s) for s in api("stations/search", params)]}


def daemon_publish(data):
    """The island activity, through the daemon (best effort)."""
    path, token = os.environ.get("SAMANTHA_SOCKET"), os.environ.get("SAMANTHA_EXTENSION_TOKEN")
    if not path or not token:
        return
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(5)
            s.connect(path)
            f = s.makefile("rwb")
            for frame in ({"v": 1, "kind": "hello", "role": "extension", "token": token},
                          {"v": 1, "kind": "request", "id": 1, "method": "events.publish",
                           "params": {"type": "radio.activity", "data": data}}):
                f.write(json.dumps(frame).encode() + b"\n")
            f.flush()
            f.readline()
    except OSError:
        pass


def stop_player():
    proc, _playing["proc"], _playing["station"] = _playing["proc"], None, None
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def watch(proc, name):
    """Clears the island when the stream ends by itself."""
    proc.wait()
    with _lock:
        if _playing["proc"] is proc:
            _playing["proc"], _playing["station"] = None, None
            daemon_publish({"key": "radio"})


def play(args):
    if args.get("id"):
        found = api(f"stations/byuuid/{urllib.parse.quote(str(args['id']), safe='')}")
        if not found:
            raise Failure("Station introuvable")
        s = station(found[0])
    elif args.get("query"):
        found = search({"query": args["query"], "limit": 1})["stations"]
        if not found:
            raise Failure(f"Aucune radio « {args['query']} »")
        s = found[0]
    else:
        raise Failure("Donne une radio (id de radio.search ou nom)")
    if not s["url"].startswith(("http://", "https://")):
        raise Failure("Cette station n'a pas de flux lisible")
    with _lock:
        stop_player()
        try:
            proc = subprocess.Popen([*PLAYER, s["url"]], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            raise Failure("ffplay (FFmpeg) n'est pas installé")
        _playing["proc"], _playing["station"] = proc, s
    threading.Thread(target=watch, args=(proc, s["name"]), daemon=True).start()
    # radio-browser counts plays to rank stations (its API etiquette).
    try:
        api(f"url/{urllib.parse.quote(s['id'], safe='')}")
    except Failure:
        pass
    daemon_publish({"key": "radio", "text": f"Radio · {s['name']}", "sub": s["country"]})
    return {"playing": s["name"], "id": s["id"], "country": s["country"]}


def stop(args):
    with _lock:
        was = (_playing["station"] or {}).get("name")
        stop_player()
    daemon_publish({"key": "radio"})
    return {"stopped": was or ""}


def now(args):
    with _lock:
        s = _playing["station"]
        alive = bool(_playing["proc"] and _playing["proc"].poll() is None)
    return {"playing": bool(s and alive), "station": (s or {}).get("name", ""),
            "country": (s or {}).get("country", "")}


# --- MCP ---------------------------------------------------------------------------

S, I = {"type": "string"}, {"type": "integer"}
TOOLS = {
    "search": (search, {
        "description": "Find live radio stations by name, country code (FR, BE, CH…) and/or "
                       "genre tag (jazz, news, classical…), most listened first; limit 1-20.",
        "inputSchema": {"type": "object", "properties": {
            "query": S, "country": S, "tag": S, "limit": I}},
        "outputSchema": {"type": "object", "properties": {"stations": {"type": "array", "items": {
            "type": "object", "properties": {"id": S, "name": S, "country": S, "tags": S,
                                             "codec": S, "bitrate": I, "url": S}}}}},
    }),
    "play": (play, {
        "description": "Play a station: id from radio.search, or a name (the most listened "
                       "match). Replaces what the radio was playing.",
        "inputSchema": {"type": "object", "properties": {"id": S, "query": S}},
    }),
    "stop": (stop, {
        "description": "Stop the radio.",
        "inputSchema": {"type": "object", "properties": {}},
    }),
    "now": (now, {
        "description": "Whether the radio plays, and which station.",
        "inputSchema": {"type": "object", "properties": {}},
    }),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Réponse inattendue ou argument "
                             "invalide"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out, "isError": False}


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None  # notification
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "samantha-radio", "version": "0.1.0"}}
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
    try:
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
    finally:
        with _lock:
            stop_player()


if __name__ == "__main__":
    main()
