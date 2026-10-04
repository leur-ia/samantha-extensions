#!/usr/bin/env python3
"""MCP stdio server of the `drives` extension (stdlib only).

USB sticks, SD cards and external disks: list them (`lsblk`), mount, unmount and
safely eject them (`udisksctl`, polkit allows the user's own removable media). A
background loop notices a drive plugged in or removed: an island activity
(`drives.activity`) and a `drives.added` event for watchers. Nothing is logged.
"""

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

PROTOCOL = "2025-06-18"
TIMEOUT = 20
COLUMNS = "NAME,PATH,LABEL,SIZE,FSTYPE,MOUNTPOINTS,RM,HOTPLUG,TRAN,MODEL,VENDOR,TYPE"
DEVICE = re.compile(r"^/dev/[a-z0-9]+$")


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def run(*argv):
    try:
        r = subprocess.run(list(argv), capture_output=True, text=True, timeout=TIMEOUT,
                           env={**os.environ, "LC_ALL": "C"})
    except FileNotFoundError:
        raise Failure(f"{argv[0]} introuvable")
    except subprocess.TimeoutExpired:
        raise Failure(f"{argv[0]} ne répond pas")
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()
        if "target is busy" in err or "busy" in err.lower():
            raise Failure("Le disque est encore utilisé (un fichier ouvert, un terminal dedans ?)")
        if "Not authorized" in err:
            raise Failure("Le système refuse cette action à Samantha (droits polkit)")
        raise Failure(f"{argv[0]}: {err[:200] or 'échec'}")
    return r.stdout


def size(n):
    n = int(n or 0)
    return f"{n / 1e9:.1f} Go" if n >= 1e9 else f"{n / 1e6:.0f} Mo"


def drives(args=None):
    """Removable disks and their mountable partitions."""
    try:
        tree = json.loads(run("lsblk", "-J", "-b", "-o", COLUMNS))["blockdevices"]
    except (ValueError, KeyError):
        raise Failure("lsblk illisible")
    rows = []
    for disk in tree:
        if disk.get("type") != "disk" or not (disk.get("rm") or disk.get("hotplug")):
            continue
        # lsblk pads vendor names with spaces.
        name = " ".join(x.strip() for x in (disk.get("vendor"), disk.get("model")) if x and x.strip()) or disk.get("name")
        parts = [p for p in disk.get("children") or [] if p.get("fstype")] or ([disk] if disk.get("fstype") else [])
        rows.append({"disk": disk["path"], "name": name, "size": size(disk.get("size")),
                     "connection": disk.get("tran") or "",
                     "partitions": [{"device": p["path"], "label": p.get("label") or "", "size": size(p.get("size")),
                                     "filesystem": p.get("fstype"),
                                     "mounted_at": next((m for m in p.get("mountpoints") or [] if m), None)}
                                    for p in parts]})
    return {"drives": rows}


def find_partition(ref):
    ref = str(ref or "").strip().lower()
    found = [(d, p) for d in drives()["drives"] for p in d["partitions"]
             if not ref or ref in (p["device"].lower(), p["label"].lower()) or ref in d["name"].lower()]
    if len(found) != 1:
        names = ", ".join(f"{p['label'] or p['device']} ({d['name']})" for d in drives()["drives"] for p in d["partitions"])
        raise Failure(("Plusieurs volumes correspondent" if found else "Aucun disque amovible ne correspond")
                      + f": {names or 'aucun branché'}")
    return found[0]


def mount(args):
    disk, part = find_partition(args.get("drive"))
    if part["mounted_at"]:
        return {"mounted_at": part["mounted_at"], "already": True}
    out = run("udisksctl", "mount", "-b", part["device"], "--no-user-interaction")
    m = re.search(r" at (.+?)\.?$", out.strip())
    return {"mounted_at": m.group(1) if m else "", "label": part["label"]}


def eject(args):
    disk, part = find_partition(args.get("drive"))
    for p in next(d for d in drives()["drives"] if d["disk"] == disk["disk"])["partitions"]:
        if p["mounted_at"]:
            run("udisksctl", "unmount", "-b", p["device"], "--no-user-interaction")
    if DEVICE.match(disk["disk"]):
        try:
            run("udisksctl", "power-off", "-b", disk["disk"], "--no-user-interaction")
        except Failure:
            return {"ejected": disk["name"], "powered_off": False}
    return {"ejected": disk["name"], "powered_off": True}


def unmount(args):
    _, part = find_partition(args.get("drive"))
    if part["mounted_at"]:
        run("udisksctl", "unmount", "-b", part["device"], "--no-user-interaction")
    return {"unmounted": part["label"] or part["device"]}


# --- plug events -------------------------------------------------------------------

def publish(kind, data):
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
                           "params": {"type": kind, "data": data}}):
                f.write(json.dumps(frame).encode() + b"\n")
            f.flush()
            f.readline()
    except OSError:
        pass


def changes(before, after):
    """(added, removed) drives between two `drives()` results, by disk path."""
    b = {d["disk"]: d for d in before}
    a = {d["disk"]: d for d in after}
    return [a[k] for k in a if k not in b], [b[k] for k in b if k not in a]


def watch_forever():
    known = None
    while True:
        try:
            now = drives()["drives"]
        except Failure:
            now = known or []
        if known is not None:
            added, removed = changes(known, now)
            for d in added:
                label = next((p["label"] for p in d["partitions"] if p["label"]), "") or d["name"]
                publish("drives.activity", {"key": d["disk"], "text": f"Disque branché · {label}",
                                            "sub": d["size"], "icon": "download", "ttl_s": 6})
                publish("drives.added", {"name": d["name"], "label": label, "size": d["size"]})
            for d in removed:
                publish("drives.activity", {"key": d["disk"]})
        known = now
        time.sleep(3)


# --- MCP ---------------------------------------------------------------------------

S = {"type": "string"}
DRIVE = {"type": "string", "description": "Label, device (/dev/sdb1) or drive name; default: the only one."}
TOOLS = {
    "list": (drives, {
        "description": "Removable drives plugged in (USB, SD, external disks), their volumes and where mounted.",
        "inputSchema": {"type": "object", "properties": {}},
    }),
    "mount": (mount, {
        "description": "Mount a removable volume; returns its folder.",
        "inputSchema": {"type": "object", "properties": {"drive": DRIVE}},
    }),
    "unmount": (unmount, {
        "description": "Unmount a volume without powering the drive off.",
        "inputSchema": {"type": "object", "properties": {"drive": DRIVE}},
    }),
    "eject": (eject, {
        "description": "Safely eject a drive: unmount all its volumes, then power it off.",
        "inputSchema": {"type": "object", "properties": {"drive": DRIVE}},
    }),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, StopIteration):
        return {"content": [{"type": "text", "text": "Réponse inattendue ou argument invalide"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out, "isError": False}


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None  # notification
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "samantha-drives", "version": "0.1.0"}}
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
    if os.environ.get("SAMANTHA_SOCKET"):
        threading.Thread(target=watch_forever, daemon=True).start()
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
