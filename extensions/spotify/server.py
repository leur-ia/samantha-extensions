#!/usr/bin/env python3
"""MCP stdio server of the `spotify` extension (stdlib only).

Run as `server.py player`: `now_playing`, `control` and `open` drive the Spotify desktop
app (flatpak com.spotify.Client) over MPRIS on the session bus with `busctl --user`
(Python has no D-Bus in its standard library). Run as `server.py catalog`: `search`
uses the official Web API with the Client Credentials flow (SPOTIFY_CLIENT_ID and
SPOTIFY_CLIENT_SECRET in the environment, from the Secret Service); the token is kept
in memory until it expires. No unofficial endpoint, no scraping. Failures are `isError`
results in French, never a crash; nothing is logged and no error carries a key or token.
"""

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = "2025-06-18"
TIMEOUT = 8          # seconds per Web API request
BUS_TIMEOUT = 5      # seconds per busctl call
BUS = ["busctl", "--user", "--json=short"]
DEST = ["org.mpris.MediaPlayer2.spotify", "/org/mpris/MediaPlayer2"]
PLAYER = "org.mpris.MediaPlayer2.Player"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"
URI = re.compile(r"^spotify:(track|album|playlist|artist|show|episode):[A-Za-z0-9]+$")
ACTIONS = {"play": "Play", "pause": "Pause", "toggle": "PlayPause",
           "next": "Next", "previous": "Previous"}
TYPES = ("track", "album", "playlist", "artist")
NOT_RUNNING = "Spotify n'est pas lancé."
NO_KEYS = ("Recherche Spotify non configurée: crée une app sur developer.spotify.com "
           "(Client Credentials; le propriétaire doit avoir Premium), puis enregistre ses "
           "clés avec `samantha provider set-key spotify-client-id` et `samantha provider "
           "set-key spotify-client-secret` (chaque clé est lue sur l'entrée standard).")


class Failure(Exception):
    """A message for the model: the call failed, say why."""


class NotRunning(Failure):
    def __init__(self):
        super().__init__(NOT_RUNNING)


# --- MPRIS over busctl -------------------------------------------------------------

def busctl(*args):
    """Run busctl against Spotify's MPRIS object; its stdout."""
    try:
        r = subprocess.run([*BUS, *args], capture_output=True, text=True,
                           timeout=BUS_TIMEOUT)
    except FileNotFoundError:
        raise Failure("busctl introuvable: impossible de parler au bus de session")
    except subprocess.TimeoutExpired:
        raise Failure("Spotify ne répond pas sur le bus de session")
    if r.returncode != 0:
        err = r.stderr.strip()
        # The bus answers ServiceUnknown / NameHasNoOwner when nobody owns the name.
        if any(s in err for s in ("not provided by any", "ServiceUnknown",
                                  "NameHasNoOwner", "was not provided")):
            raise NotRunning()
        raise Failure(f"Échec MPRIS: {err[:200] or 'busctl a échoué'}")
    return r.stdout


def prop(name):
    """One Player property, unwrapped from busctl's {"type": …, "data": …}."""
    out = busctl("get-property", *DEST, PLAYER, name)
    try:
        return json.loads(out)["data"]
    except (ValueError, KeyError, TypeError):
        raise Failure("Réponse MPRIS illisible")


def unwrap(v):
    """A variant inside a{sv} is again {"type": …, "data": …}."""
    return v.get("data") if isinstance(v, dict) and "type" in v else v


def track_uri(trackid):
    """`/com/spotify/track/ID` (or an older `spotify:track:ID`) to a spotify: URI."""
    m = re.match(r"^/com/spotify/(\w+)/(\w+)$", trackid or "")
    if m:
        return f"spotify:{m.group(1)}:{m.group(2)}"
    return trackid if URI.match(trackid or "") else ""


def seconds(us):
    return round(us / 1_000_000) if isinstance(us, (int, float)) and us > 0 else None


def now_playing(args):
    try:
        state = prop("PlaybackStatus")
    except NotRunning:
        return {"running": False, "state": "stopped", "title": "", "artists": "",
                "album": "", "uri": "", "position_s": None, "length_s": None}
    meta = {k: unwrap(v) for k, v in (prop("Metadata") or {}).items()}
    try:
        position = seconds(prop("Position"))  # Spotify often reports 0 or nothing
    except Failure:
        position = None
    artists = meta.get("xesam:artist") or []
    return {
        "running": True,
        "state": str(state).lower(),  # playing | paused | stopped
        "title": meta.get("xesam:title") or "",
        "artists": ", ".join(artists) if isinstance(artists, list) else str(artists),
        "album": meta.get("xesam:album") or "",
        "uri": track_uri(meta.get("mpris:trackid")),
        "position_s": position,
        "length_s": seconds(meta.get("mpris:length")),
    }


def control(args):
    action = args.get("action")
    if action not in ACTIONS:
        raise Failure(f"Action inconnue: {action!r} (play, pause, toggle, next, previous)")
    busctl("call", *DEST, PLAYER, ACTIONS[action])
    return {"done": action}


def open_uri(args):
    uri = args.get("id") or args.get("uri")
    if not isinstance(uri, str) or not URI.match(uri):
        raise Failure("URI invalide: attendu spotify:track|album|playlist|artist|show|"
                      "episode:<id> (prends l'uri renvoyée par spotify.search)")
    busctl("call", *DEST, PLAYER, "OpenUri", "s", uri)
    return {"opened": uri}


# --- Web API (Client Credentials) ---------------------------------------------------

_token = {"value": None, "expires": 0.0}


def http_json(req, what):
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            _token["value"] = None
        if e.code == 429:
            raise Failure("Spotify limite les requêtes: réessaie dans un moment")
        if e.code in (400, 401) and what == "auth":
            raise Failure("Spotify refuse les identifiants de l'app (spotify-client-id / "
                          "spotify-client-secret): vérifie-les, puis réenregistre-les avec "
                          "`samantha provider set-key`")
        raise Failure(f"Spotify a refusé la requête ({e.code})")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise Failure("Spotify injoignable (réseau ?)")
    except ValueError:
        raise Failure("Spotify a répondu quelque chose d'illisible")


def token():
    if _token["value"] and time.monotonic() < _token["expires"]:
        return _token["value"]
    cid = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        raise Failure(NO_KEYS)
    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    req = urllib.request.Request(
        TOKEN_URL, data=b"grant_type=client_credentials", method="POST",
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"})
    data = http_json(req, "auth")
    try:
        value, ttl = data["access_token"], int(data.get("expires_in", 3600))
    except (KeyError, TypeError, ValueError):
        raise Failure("Spotify a répondu quelque chose d'illisible")
    _token.update(value=value, expires=time.monotonic() + max(ttl - 60, 0))
    return value


def row(kind, item):
    artists = item.get("artists") or []
    if kind == "playlist":
        by = (item.get("owner") or {}).get("display_name") or ""
    else:
        by = ", ".join(a.get("name", "") for a in artists if isinstance(a, dict))
    uri = item.get("uri") or ""
    out = {"id": uri, "kind": kind, "name": item.get("name") or "", "artists": by, "uri": uri}
    if kind == "track":
        out["album"] = (item.get("album") or {}).get("name") or ""
    return out


def search(args):
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        raise Failure("Recherche vide: donne un titre, un artiste ou un album")
    kind = args.get("kind") or args.get("type") or "track"
    if kind == "station":
        return {"type": kind, "results": []}  # radio stations aren't Spotify's
    if kind not in TYPES:
        raise Failure(f"Type inconnu: {kind!r} (track, album, playlist, artist)")
    limit = max(1, min(10, int(args.get("limit") or 5)))  # the API caps at 10
    params = urllib.parse.urlencode({"q": query.strip(), "type": kind, "limit": limit})
    req = urllib.request.Request(f"{SEARCH_URL}?{params}",
                                 headers={"Authorization": f"Bearer {token()}"})
    data = http_json(req, "search")
    items = (data.get(kind + "s") or {}).get("items") or []
    # Playlist results can contain null entries.
    return {"type": kind, "results": [row(kind, i) for i in items if isinstance(i, dict)]}


# --- MCP ---------------------------------------------------------------------------

S = {"type": "string"}
N = {"type": ["integer", "null"]}
PLAYER_TOOLS = {
    "now_playing": (now_playing, {
        "description": "What the Spotify desktop app is playing: title, artists, album, "
                       "state (playing/paused/stopped), position and length in seconds "
                       "when known. running is false when Spotify is not started.",
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {"type": "object", "properties": {
            "running": {"type": "boolean"}, "state": S, "title": S, "artists": S,
            "album": S, "uri": S, "position_s": N, "length_s": N}},
    }),
    "control": (control, {
        "description": "Play, pause, toggle play/pause, next or previous track in Spotify.",
        "inputSchema": {"type": "object", "required": ["action"], "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)}}},
        "outputSchema": {"type": "object", "properties": {"done": S}},
    }),
    "play": (open_uri, {
        "description": "Play a spotify: URI (track, album, playlist, artist, show, "
                       "episode) in the Spotify app, e.g. one from spotify.search.",
        "inputSchema": {"type": "object", "required": ["id"], "properties": {
            "id": {"type": "string", "pattern": URI.pattern}}},
        "outputSchema": {"type": "object", "properties": {"opened": S}},
    }),
}
CATALOG_TOOLS = {
    "search": (search, {
        "description": "Search the Spotify catalog (official Web API). type track "
                       "(default), album, playlist or artist; limit 1 to 10 (default 5). "
                       "Rows have name, artists (playlist: owner), uri (and album for "
                       "tracks); pass a uri to spotify.open to play it.",
        "inputSchema": {"type": "object", "required": ["query"], "properties": {
            "query": S, "type": {"type": "string", "enum": list(TYPES)},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10}}},
        "outputSchema": {"type": "object", "properties": {
            "type": S, "results": {"type": "array", "items": {"type": "object",
                "properties": {"name": S, "artists": S, "uri": S, "album": S}}}}},
    }),
}
TOOLS = {**PLAYER_TOOLS, **CATALOG_TOOLS}  # narrowed by main() to the role run


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Réponse de Spotify inattendue "
                             "ou argument invalide"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out, "isError": False}


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None  # notification
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "samantha-spotify", "version": "0.1.0"}}
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
    global TOOLS
    role = sys.argv[1] if len(sys.argv) > 1 else "player"
    TOOLS = CATALOG_TOOLS if role == "catalog" else PLAYER_TOOLS
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
