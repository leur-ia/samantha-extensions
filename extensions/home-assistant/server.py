#!/usr/bin/env python3
"""MCP stdio server of the `home-assistant` extension (stdlib only).

The user's Home Assistant through its REST API: the address in
~/.config/samantha/home-assistant.toml (`url = "http://homeassistant.local:8123"`), a
long-lived access token in HA_TOKEN (Secret Service item `home-assistant`). Everyday
controls (lights, switches, fans, blinds, climate, media players, scenes) are one tool;
anything else (locks, alarm, garage doors, scripts) goes through `call_service`, which
the user confirms each time. Failures are `isError` results; nothing is logged.
"""

import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = "2025-06-18"
TIMEOUT = 10
ENTITY = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
NAME = re.compile(r"^[a-z_]+$")
# Domains `control` may act on, and what each action calls there.
ACTIONS = {
    "light": {"on": "turn_on", "off": "turn_off", "toggle": "toggle"},
    "switch": {"on": "turn_on", "off": "turn_off", "toggle": "toggle"},
    "fan": {"on": "turn_on", "off": "turn_off", "toggle": "toggle"},
    "input_boolean": {"on": "turn_on", "off": "turn_off", "toggle": "toggle"},
    "media_player": {"on": "turn_on", "off": "turn_off", "toggle": "media_play_pause"},
    "climate": {"on": "turn_on", "off": "turn_off"},
    "cover": {"open": "open_cover", "close": "close_cover", "stop": "stop_cover"},
    "scene": {"on": "turn_on"},
}
# Covers that guard the house are confirmed through call_service.
GUARDING = {"garage", "gate", "door"}
SETUP = ("Home Assistant n'est pas configuré: écris url = \"http://homeassistant.local:8123\" "
         "dans ~/.config/samantha/home-assistant.toml, crée un jeton d'accès longue durée "
         "(profil Home Assistant > Sécurité) et enregistre-le avec "
         "`samantha provider set-key home-assistant`.")


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def config():
    home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    try:
        with open(os.path.join(home, "samantha", "home-assistant.toml"), "rb") as f:
            url = tomllib.load(f).get("url")
    except (OSError, tomllib.TOMLDecodeError):
        url = None
    token = os.environ.get("HA_TOKEN", "").strip()
    if not isinstance(url, str) or not url.startswith(("http://", "https://")) or not token:
        raise Failure(SETUP)
    return url.rstrip("/"), token


def api(method, path, body=None):
    url, token = config()
    req = urllib.request.Request(url + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise Failure("Home Assistant refuse le jeton. " + SETUP)
        if e.code == 404:
            raise Failure("Introuvable dans Home Assistant (entité ou service inconnu)")
        if e.code == 400:
            raise Failure("Home Assistant a refusé ces paramètres")
        raise Failure(f"Home Assistant a refusé la requête ({e.code})")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise Failure("Home Assistant injoignable (réseau, adresse ?)")
    except ValueError:
        raise Failure("Home Assistant a répondu quelque chose d'illisible")


def summary(s):
    a = s.get("attributes") or {}
    out = {"entity_id": s.get("entity_id", ""), "name": a.get("friendly_name") or s.get("entity_id", ""),
           "state": s.get("state", "")}
    for k in ("unit_of_measurement", "device_class", "brightness", "current_temperature",
              "temperature", "current_position", "media_title"):
        if a.get(k) is not None:
            out[k] = a[k]
    return out


def area_ids(area):
    rendered = api("POST", "/api/template", {"template": "{{ area_entities(%s) | tojson }}" % json.dumps(area)})
    try:
        return set(json.loads(rendered)) if isinstance(rendered, str) else set(rendered or [])
    except ValueError:
        return set()


def entities(args):
    domain = str(args.get("domain") or "").strip().lower()
    text = str(args.get("search") or "").strip().lower()
    states = api("GET", "/api/states") or []
    wanted = area_ids(str(args["area"])) if args.get("area") else None
    rows = []
    for s in states:
        eid = s.get("entity_id", "")
        name = str((s.get("attributes") or {}).get("friendly_name", "")).lower()
        if domain and not eid.startswith(domain + "."):
            continue
        if text and text not in eid and text not in name:
            continue
        if wanted is not None and eid not in wanted:
            continue
        rows.append(summary(s))
    limit = max(1, min(200, int(args.get("limit") or 50)))
    return {"count": len(rows), "entities": rows[:limit]}


def state(args):
    eid = str(args.get("entity_id") or "")
    if not ENTITY.match(eid):
        raise Failure("entity_id invalide (ex. light.salon)")
    return summary(api("GET", f"/api/states/{eid}"))


def control(args):
    eid = str(args.get("entity_id") or "")
    if not ENTITY.match(eid):
        raise Failure("entity_id invalide (ex. light.salon)")
    domain = eid.split(".")[0]
    action = str(args.get("action") or "")
    if domain not in ACTIONS:
        raise Failure(f"{domain} se commande avec call_service (confirmé par l'utilisateur)")
    current = api("GET", f"/api/states/{eid}")
    if domain == "cover" and (current.get("attributes") or {}).get("device_class") in GUARDING:
        raise Failure("Porte, portail ou garage: passe par call_service (confirmé par l'utilisateur)")
    data = {"entity_id": eid}
    if action == "set":
        if domain == "light" and args.get("brightness") is not None:
            service, data["brightness_pct"] = "turn_on", max(0, min(100, int(args["brightness"])))
        elif domain == "climate" and args.get("temperature") is not None:
            service, data["temperature"] = "set_temperature", float(args["temperature"])
        elif domain == "cover" and args.get("position") is not None:
            service, data["position"] = "set_cover_position", max(0, min(100, int(args["position"])))
        elif domain == "media_player" and args.get("volume") is not None:
            service, data["volume_level"] = "volume_set", max(0, min(100, int(args["volume"]))) / 100
        else:
            raise Failure("set: brightness (lumière), temperature (climat), position (volet) ou volume (lecteur)")
    elif action in ACTIONS[domain]:
        service = ACTIONS[domain][action]
    else:
        raise Failure(f"Action {action!r} impossible pour {domain}: {', '.join(ACTIONS[domain])} ou set")
    api("POST", f"/api/services/{domain}/{service}", data)
    time.sleep(0.3)  # let the state settle before reading it back
    return {"done": f"{domain}.{service}", **summary(api("GET", f"/api/states/{eid}"))}


def call_service(args):
    domain, service = str(args.get("domain") or ""), str(args.get("service") or "")
    if not NAME.match(domain) or not NAME.match(service):
        raise Failure("domain et service: des noms comme lock et unlock")
    data = args.get("data") or {}
    if not isinstance(data, dict):
        raise Failure("data: un objet (ex. {\"entity_id\": \"lock.entree\"})")
    changed = api("POST", f"/api/services/{domain}/{service}", data) or []
    return {"done": f"{domain}.{service}", "changed": [summary(s) for s in changed][:20]}


# --- MCP ---------------------------------------------------------------------------

S, I, N = {"type": "string"}, {"type": "integer"}, {"type": "number"}
TOOLS = {
    "entities": (entities, {
        "description": "Home Assistant entities with their state: filter by domain (light, "
                       "sensor, climate…), area (Salon, Cuisine…) and/or a search on id or name.",
        "inputSchema": {"type": "object", "properties": {
            "domain": S, "area": S, "search": S, "limit": I}},
    }),
    "state": (state, {
        "description": "One entity's state and main attributes.",
        "inputSchema": {"type": "object", "required": ["entity_id"], "properties": {"entity_id": S}},
    }),
    "control": (control, {
        "description": "Everyday control: on/off/toggle (light, switch, fan, input_boolean, "
                       "media_player, climate), open/close/stop (blinds), on (scene), or set "
                       "with brightness 0-100, temperature, position 0-100 or volume 0-100. "
                       "Returns the new state.",
        "inputSchema": {"type": "object", "required": ["entity_id", "action"], "properties": {
            "entity_id": S, "action": {"type": "string", "enum": ["on", "off", "toggle", "open", "close", "stop", "set"]},
            "brightness": N, "temperature": N, "position": N, "volume": N}},
    }),
    "call_service": (call_service, {
        "description": "Any Home Assistant service (lock.unlock, alarm_control_panel.alarm_arm_away, "
                       "cover.open_cover on a garage, script.turn_on…) with its data; the user "
                       "confirms each call.",
        "inputSchema": {"type": "object", "required": ["domain", "service"], "properties": {
            "domain": S, "service": S, "data": {"type": "object"}}},
    }),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Réponse de Home Assistant inattendue "
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
                  "serverInfo": {"name": "samantha-home-assistant", "version": "0.1.0"}}
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
