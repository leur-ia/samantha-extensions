#!/usr/bin/env python3
"""MCP stdio server of the `phone` extension (stdlib only).

The user's phone through KDE Connect (the KDE Connect app on Android or iOS, paired
with this computer): ring it, its battery, the notifications it shows, send an SMS,
share a file or link, send it a message. `kdeconnect-cli` in the C locale (its output
is parsed) and `busctl --user` for the battery, both reaching the kdeconnectd daemon on
the session bus. The phone's notifications also reach the desktop (the
`notifications` extension keeps them). Failures are `isError` results; nothing is
logged.
"""

import json
import os
import re
import subprocess
import sys

PROTOCOL = "2025-06-18"
TIMEOUT = 15
CLI = "kdeconnect-cli"
DEVICE_LINE = re.compile(r"^- (.+): ([0-9A-Za-z_]+)\s*(?:\((.*)\))?\s*$")
PHONE = re.compile(r"^\+?[0-9 ().-]{3,20}$")
SETUP = ("Aucun téléphone appairé: installe l'app KDE Connect sur le téléphone, sur le même "
         "réseau, puis appaire-le (outil phone.pair, à accepter sur le téléphone).")


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def run(*args, timeout=TIMEOUT):
    env = {**os.environ, "LC_ALL": "C", "LANGUAGE": "C"}
    try:
        r = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        raise Failure(f"{args[0]} introuvable: KDE Connect n'est pas installé")
    except subprocess.TimeoutExpired:
        raise Failure("KDE Connect ne répond pas")
    if r.returncode != 0:
        raise Failure(f"KDE Connect: {(r.stderr or r.stdout).strip()[:200] or 'échec'}")
    return r.stdout


def devices(args=None):
    if (args or {}).get("refresh"):
        run(CLI, "--refresh")
    rows = []
    for line in run(CLI, "-l").splitlines():
        m = DEVICE_LINE.match(line.strip())
        if m:
            status = m.group(3) or ""
            rows.append({"id": m.group(2), "name": m.group(1),
                         "paired": "paired" in status, "reachable": "reachable" in status})
    return {"devices": rows}


def pick(ref, need_reachable=True):
    rows = [d for d in devices()["devices"] if d["paired"]]
    if not rows:
        raise Failure(SETUP)
    ref = str(ref or "").strip().lower()
    match = [d for d in rows if not ref or ref == d["id"].lower() or ref in d["name"].lower()]
    if len(match) > 1 and not ref:
        match = [d for d in match if d["reachable"]] or match
    if len(match) != 1:
        names = ", ".join(d["name"] for d in rows)
        raise Failure(("Plusieurs téléphones: précise lequel" if match else "Aucun téléphone ne correspond")
                      + f" ({names})")
    d = match[0]
    if need_reachable and not d["reachable"]:
        raise Failure(f"{d['name']} n'est pas joignable (même Wi-Fi ? app KDE Connect ouverte ?)")
    return d


def battery(args):
    d = pick(args.get("device"))
    base = ["busctl", "--user", "--json=short", "get-property", "org.kde.kdeconnect",
            f"/modules/kdeconnect/devices/{d['id']}/battery", "org.kde.kdeconnect.device.battery"]
    try:
        charge = json.loads(run(*base, "charge"))["data"]
        charging = json.loads(run(*base, "isCharging"))["data"]
    except (ValueError, KeyError, TypeError):
        raise Failure("Batterie illisible (le module Batterie est-il actif dans KDE Connect ?)")
    return {"device": d["name"], "charge": charge, "charging": bool(charging)}


def ring(args):
    d = pick(args.get("device"))
    run(CLI, "--ring", "-d", d["id"])
    return {"ringing": d["name"]}


def notifications(args):
    d = pick(args.get("device"))
    rows = []
    for line in run(CLI, "--list-notifications", "-d", d["id"]).splitlines():
        line = line.strip()
        if line.startswith("- ") and ": " in line:
            app, text = line[2:].split(": ", 1)
            rows.append({"app": app, "text": text[:500]})
    return {"device": d["name"], "notifications": rows}


def send_sms(args):
    to, text = str(args.get("to") or "").strip(), str(args.get("text") or "").strip()
    if not PHONE.match(to):
        raise Failure("Numéro invalide (ex. +33612345678)")
    if not text:
        raise Failure("Message vide")
    d = pick(args.get("device"))
    run(CLI, "--send-sms", text, "--destination", re.sub(r"[ ().-]", "", to), "-d", d["id"])
    return {"sent": True, "to": to, "via": d["name"]}


def share(args):
    what = str(args.get("what") or "").strip()
    if not what:
        raise Failure("Donne un fichier (chemin absolu) ou un lien")
    if not (what.startswith(("http://", "https://")) or os.path.isabs(what)):
        raise Failure("Un chemin absolu ou un lien http(s)")
    d = pick(args.get("device"))
    run(CLI, "--share", what, "-d", d["id"])
    return {"shared": what, "to": d["name"]}


def message(args):
    text = str(args.get("text") or "").strip()[:300]
    if not text:
        raise Failure("Message vide")
    d = pick(args.get("device"))
    run(CLI, "--ping-msg", text, "-d", d["id"])
    return {"sent": text, "to": d["name"]}


def pair(args):
    rows = devices({"refresh": True})["devices"]
    ref = str(args.get("device") or "").strip().lower()
    match = [d for d in rows if d["reachable"] and not d["paired"] and (not ref or ref in d["name"].lower())]
    if len(match) != 1:
        seen = ", ".join(d["name"] for d in rows if not d["paired"]) or "aucun"
        raise Failure(f"Téléphone à appairer introuvable ou ambigu (vus: {seen}); l'app KDE Connect "
                      "doit être ouverte sur le même réseau")
    run(CLI, "--pair", "-d", match[0]["id"])
    return {"requested": match[0]["name"], "next": "Accepter la demande sur le téléphone"}


# --- MCP ---------------------------------------------------------------------------

S = {"type": "string"}
DEVICE = {"type": "string", "description": "Phone name or id; default: the paired one."}
TOOLS = {
    "devices": (devices, {
        "description": "Phones and tablets KDE Connect knows, paired and reachable or not "
                       "(refresh: search the network first).",
        "inputSchema": {"type": "object", "properties": {"refresh": {"type": "boolean"}}},
    }),
    "battery": (battery, {
        "description": "The phone's battery level and whether it is charging.",
        "inputSchema": {"type": "object", "properties": {"device": DEVICE}},
    }),
    "ring": (ring, {
        "description": "Ring the phone to find it (even when silent).",
        "inputSchema": {"type": "object", "properties": {"device": DEVICE}},
    }),
    "notifications": (notifications, {
        "description": "Notifications currently shown on the phone (app and text).",
        "inputSchema": {"type": "object", "properties": {"device": DEVICE}},
    }),
    "send_sms": (send_sms, {
        "description": "Send an SMS from the phone to a number.",
        "inputSchema": {"type": "object", "required": ["to", "text"], "properties": {
            "to": S, "text": S, "device": DEVICE}},
    }),
    "share": (share, {
        "description": "Send a file (absolute path) or a link to the phone.",
        "inputSchema": {"type": "object", "required": ["what"], "properties": {"what": S, "device": DEVICE}},
    }),
    "message": (message, {
        "description": "Show a short message on the phone (a KDE Connect ping).",
        "inputSchema": {"type": "object", "required": ["text"], "properties": {"text": S, "device": DEVICE}},
    }),
    "pair": (pair, {
        "description": "Ask a phone running KDE Connect on the same network to pair (accepted on the phone).",
        "inputSchema": {"type": "object", "properties": {"device": DEVICE}},
    }),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Réponse de KDE Connect inattendue ou "
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
                  "serverInfo": {"name": "samantha-phone", "version": "0.1.0"}}
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
