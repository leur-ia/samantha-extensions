#!/usr/bin/env python3
"""MCP stdio server of the `audio` extension (stdlib only).

Sound outputs and inputs through PipeWire (`pw-dump` to list, `wpctl` to change):
which device plays, volume, mute. Bluetooth audio devices through `bluetoothctl`
(paired devices, connect, disconnect). Failures are `isError` results, never a crash;
nothing is logged.
"""

import json
import re
import subprocess
import sys

PROTOCOL = "2025-06-18"
TIMEOUT = 8
MAC = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
KINDS = {"Audio/Sink": "output", "Audio/Source": "input"}
DEFAULT = {"output": "@DEFAULT_AUDIO_SINK@", "input": "@DEFAULT_AUDIO_SOURCE@"}


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def run(*argv, timeout=TIMEOUT):
    try:
        r = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise Failure(f"{argv[0]} introuvable")
    except subprocess.TimeoutExpired:
        raise Failure(f"{argv[0]} ne répond pas")
    if r.returncode != 0:
        raise Failure(f"{argv[0]}: {(r.stderr or r.stdout).strip()[:200] or 'échec'}")
    return r.stdout


def volume_of(target):
    """(level 0-100, muted) from `wpctl get-volume`: "Volume: 0.40 [MUTED]"."""
    out = run("wpctl", "get-volume", str(target))
    m = re.search(r"Volume:\s*([\d.]+)", out)
    return (round(float(m.group(1)) * 100) if m else None), "[MUTED]" in out


def devices(args=None):
    try:
        dump = json.loads(run("pw-dump"))
    except ValueError:
        raise Failure("Réponse de PipeWire illisible")
    defaults = {}
    for o in dump:
        if (o.get("props") or {}).get("metadata.name") == "default":
            for m in o.get("metadata") or []:
                value = m.get("value")
                if isinstance(value, dict):
                    defaults[m.get("key")] = value.get("name")
    rows = []
    for o in dump:
        props = (o.get("info") or {}).get("props") or {}
        kind = KINDS.get(props.get("media.class"))
        if not kind:
            continue
        name = props.get("node.name", "")
        key = "default.audio.sink" if kind == "output" else "default.audio.source"
        level, muted = volume_of(o["id"])
        rows.append({"id": o["id"], "kind": kind, "name": props.get("node.description") or name,
                     "node": name, "default": defaults.get(key) == name,
                     "volume": level, "muted": muted,
                     "bluetooth": name.startswith("bluez_")})
    return {"devices": rows}


def find(ref, kind):
    rows = [d for d in devices()["devices"] if d["kind"] == kind]
    ref = str(ref or "").strip().lower()
    if ref.isdigit():
        match = [d for d in rows if d["id"] == int(ref)]
    else:
        match = [d for d in rows if ref and (ref in d["name"].lower() or ref in d["node"].lower())]
    if len(match) != 1:
        names = ", ".join(d["name"] for d in rows) or "aucun"
        raise Failure(("Plusieurs appareils correspondent" if match else "Aucun appareil ne correspond")
                      + f" ({kind}s: {names})")
    return match[0]


def use(args):
    kind = args.get("kind") or "output"
    if kind not in DEFAULT:
        raise Failure("kind: output ou input")
    d = find(args.get("device"), kind)
    run("wpctl", "set-default", str(d["id"]))
    return {"default": d["name"], "kind": kind}


def volume(args):
    kind = args.get("kind") or "output"
    if kind not in DEFAULT:
        raise Failure("kind: output ou input")
    target = find(args["device"], kind)["id"] if args.get("device") else DEFAULT[kind]
    if args.get("level") is not None or args.get("change") is not None:
        try:
            if args.get("level") is not None:
                level = float(args["level"])
            else:
                level = (volume_of(target)[0] or 0) + float(args["change"])
        except (TypeError, ValueError):
            raise Failure("Niveau invalide: un nombre de 0 à 100")
        # Above 100 % distorts: capped at 100.
        run("wpctl", "set-volume", str(target), f"{max(0, min(100, level)) / 100:.2f}")
    mute = args.get("mute")
    if mute is not None:
        if mute not in (True, False, "toggle"):
            raise Failure("mute: true, false ou toggle")
        run("wpctl", "set-mute", str(target), {True: "1", False: "0"}.get(mute, "toggle"))
    level, muted = volume_of(target)
    return {"kind": kind, "volume": level, "muted": muted}


def bluetooth(args):
    out = run("bluetoothctl", "devices", "Paired")
    rows = []
    for line in out.splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3 and parts[0] == "Device" and MAC.match(parts[1]):
            info = run("bluetoothctl", "info", parts[1])
            rows.append({"address": parts[1], "name": parts[2],
                         "connected": "Connected: yes" in info,
                         "audio": "Audio Sink" in info or "Headset" in info or "audio-" in info})
    return {"devices": rows}


def bluetooth_connect(args):
    ref = str(args.get("device") or "").strip()
    rows = bluetooth({})["devices"]
    match = [d for d in rows if ref.upper() == d["address"] or (ref and ref.lower() in d["name"].lower())]
    if len(match) != 1:
        names = ", ".join(d["name"] for d in rows) or "aucun appareil appairé"
        raise Failure(("Plusieurs appareils correspondent" if match else "Aucun appareil appairé ne correspond")
                      + f" ({names})")
    d, on = match[0], args.get("connect", True) is not False
    if on:
        run("bluetoothctl", "power", "on")
    run("bluetoothctl", "connect" if on else "disconnect", d["address"], timeout=20)
    return {"device": d["name"], "connected": on}


# --- MCP ---------------------------------------------------------------------------

S = {"type": "string"}
KIND = {"type": "string", "enum": ["output", "input"], "description": "Default: output."}
DEVICE = {"type": "object", "properties": {
    "id": {"type": "integer"}, "kind": S, "name": S, "node": S, "default": {"type": "boolean"},
    "volume": {"type": ["integer", "null"]}, "muted": {"type": "boolean"}, "bluetooth": {"type": "boolean"}}}
TOOLS = {
    "devices": (devices, {
        "description": "Sound outputs (speakers, headphones, HDMI) and inputs (microphones): "
                       "which is the default, volume, mute.",
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {"type": "object", "properties": {"devices": {"type": "array", "items": DEVICE}}},
    }),
    "use": (use, {
        "description": "Make a device the default output or input (by name fragment or id).",
        "inputSchema": {"type": "object", "required": ["device"], "properties": {
            "device": S, "kind": KIND}},
    }),
    "volume": (volume, {
        "description": "Set the volume (level 0-100) or change it (change: +10, -10), mute "
                       "(true, false, toggle); the default device unless one is named. "
                       "Without arguments it reads the volume.",
        "inputSchema": {"type": "object", "properties": {
            "level": {"type": "number"}, "change": {"type": "number"},
            "mute": {"type": ["boolean", "string"]}, "device": S, "kind": KIND}},
    }),
    "bluetooth": (bluetooth, {
        "description": "Paired Bluetooth devices and whether each is connected.",
        "inputSchema": {"type": "object", "properties": {}},
    }),
    "bluetooth_connect": (bluetooth_connect, {
        "description": "Connect (or with connect: false, disconnect) a paired Bluetooth device "
                       "by name fragment or address.",
        "inputSchema": {"type": "object", "required": ["device"], "properties": {
            "device": S, "connect": {"type": "boolean"}}},
    }),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Réponse audio inattendue ou "
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
                  "serverInfo": {"name": "samantha-audio", "version": "0.1.0"}}
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
