#!/usr/bin/env python3
"""MCP stdio server of the `media` extension (stdlib only).

Any media player on the session bus that speaks MPRIS (Spotify, a browser, YouTube
Music, Rhythmbox, VLC, mpv with its plugin…), through `busctl --user` (Python has no
D-Bus in its standard library). Without a `player` argument the tools act on the
player that is playing, else the first one found. Failures are `isError` results,
never a crash; nothing is logged.
"""

import json
import subprocess
import sys

PROTOCOL = "2025-06-18"
BUS = ["busctl", "--user", "--json=short"]
PREFIX = "org.mpris.MediaPlayer2."
PATH = "/org/mpris/MediaPlayer2"
PLAYER = "org.mpris.MediaPlayer2.Player"
TIMEOUT = 5
ACTIONS = {"play": "Play", "pause": "Pause", "toggle": "PlayPause", "next": "Next",
           "previous": "Previous", "stop": "Stop"}


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def busctl(*args):
    try:
        r = subprocess.run([*BUS, *args], capture_output=True, text=True, timeout=TIMEOUT)
    except FileNotFoundError:
        raise Failure("busctl introuvable: impossible de parler au bus de session")
    except subprocess.TimeoutExpired:
        raise Failure("Le lecteur ne répond pas")
    if r.returncode != 0:
        raise Failure(f"Échec MPRIS: {r.stderr.strip()[:200] or 'busctl a échoué'}")
    return r.stdout


def unwrap(v):
    return v.get("data") if isinstance(v, dict) and "type" in v else v


def prop(bus_name, name, interface=PLAYER):
    try:
        return unwrap(json.loads(busctl("get-property", bus_name, PATH, interface, name)))
    except (Failure, ValueError):
        return None


def names():
    try:
        listed = json.loads(busctl("list"))
    except ValueError:
        raise Failure("Réponse du bus illisible")
    return sorted(n["name"] for n in listed if str(n.get("name", "")).startswith(PREFIX))


def describe(bus_name):
    meta = {k: unwrap(v) for k, v in (prop(bus_name, "Metadata") or {}).items()}
    artists = meta.get("xesam:artist") or []
    length = meta.get("mpris:length")
    return {
        "player": bus_name[len(PREFIX):],
        "name": prop(bus_name, "Identity", "org.mpris.MediaPlayer2") or bus_name[len(PREFIX):],
        "state": str(prop(bus_name, "PlaybackStatus") or "Stopped").lower(),
        "title": meta.get("xesam:title") or "",
        "artists": ", ".join(artists) if isinstance(artists, list) else str(artists),
        "album": meta.get("xesam:album") or "",
        "length_s": round(length / 1e6) if isinstance(length, (int, float)) and length > 0 else None,
    }


def pick(player):
    """The bus name for `player` (a name fragment), else the playing one, else the first."""
    found = names()
    if not found:
        raise Failure("Aucun lecteur multimédia n'est ouvert")
    if player:
        p = str(player).lower()
        match = [n for n in found if p in n.lower()]
        if not match:
            raise Failure(f"Aucun lecteur « {player} »: " + ", ".join(n[len(PREFIX):] for n in found))
        return match[0]
    playing = [n for n in found if str(prop(n, "PlaybackStatus")).lower() == "playing"]
    return (playing or found)[0]


def players(args):
    return {"players": [describe(n) for n in names()]}


def now_playing(args):
    return describe(pick(args.get("player")))


def control(args):
    action = args.get("action")
    if action not in ACTIONS:
        raise Failure(f"Action inconnue: {action!r} ({', '.join(ACTIONS)})")
    target = pick(args.get("player"))
    busctl("call", target, PATH, PLAYER, ACTIONS[action])
    return {"done": action, "player": target[len(PREFIX):]}


def volume(args):
    """MPRIS Volume, 0 to 100 (players that support it)."""
    try:
        level = max(0.0, min(100.0, float(args.get("level"))))
    except (TypeError, ValueError):
        raise Failure("Niveau invalide: un nombre de 0 à 100")
    target = pick(args.get("player"))
    busctl("set-property", target, PATH, PLAYER, "Volume", "d", str(level / 100))
    return {"volume": round(level), "player": target[len(PREFIX):]}


# --- MCP ---------------------------------------------------------------------------

S = {"type": "string"}
PLAYER_ARG = {"type": "string", "description": "Player name fragment (spotify, firefox…); default: the one playing."}
TRACK = {"type": "object", "properties": {
    "player": S, "name": S, "state": S, "title": S, "artists": S, "album": S,
    "length_s": {"type": ["integer", "null"]}}}
TOOLS = {
    "players": (players, {
        "description": "Media players open (MPRIS) and what each is playing.",
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {"type": "object", "properties": {"players": {"type": "array", "items": TRACK}}},
    }),
    "now_playing": (now_playing, {
        "description": "What is playing: title, artists, album, state, player.",
        "inputSchema": {"type": "object", "properties": {"player": PLAYER_ARG}},
        "outputSchema": TRACK,
    }),
    "control": (control, {
        "description": "Play, pause, toggle, next, previous or stop a media player.",
        "inputSchema": {"type": "object", "required": ["action"], "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)}, "player": PLAYER_ARG}},
    }),
    "volume": (volume, {
        "description": "A player's own volume, 0 to 100 (not the system volume).",
        "inputSchema": {"type": "object", "required": ["level"], "properties": {
            "level": {"type": "number"}, "player": PLAYER_ARG}},
    }),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Réponse du lecteur inattendue ou "
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
                  "serverInfo": {"name": "samantha-media", "version": "0.1.0"}}
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
