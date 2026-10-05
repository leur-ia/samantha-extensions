#!/usr/bin/env python3
"""MCP stdio server of the `rclone` extension (stdlib only).

A provider of Samantha's `files` front: every remote the user configured with
`rclone config` (Google Drive, Dropbox, OneDrive, WebDAV/kDrive, S3…), through the
rclone command (in the image). Methods: places, list, search, download, upload.
Downloads go to ~/Downloads. rclone keeps its own configuration and tokens in
~/.config/rclone; nothing is logged.
"""

import json
import os
import re
import subprocess
import sys

PROTOCOL = "2025-06-18"
TIMEOUT = 90
DOWNLOADS = os.path.expanduser("~/Downloads")
REMOTE = re.compile(r"^[\w.@ -]+$")
SETUP = "Aucun stockage configuré: lance `rclone config` dans un terminal pour ajouter Google Drive, Dropbox, OneDrive…"


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def rclone(*args, timeout=TIMEOUT):
    try:
        r = subprocess.run(["rclone", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise Failure("rclone n'est pas installé")
    except subprocess.TimeoutExpired:
        raise Failure("Le stockage ne répond pas (délai dépassé)")
    if r.returncode != 0:
        err = (r.stderr.strip().splitlines() or ["échec"])[-1]
        if "didn't find section in config file" in err or "not found in config" in err:
            raise Failure(f"Stockage inconnu. {SETUP}")
        raise Failure(f"rclone: {err[:250]}")
    return r.stdout


def remotes():
    out = json.loads(rclone("config", "dump") or "{}")
    if not out:
        raise Failure(SETUP)
    return out


def target(place, path=""):
    if not REMOTE.match(str(place or "")):
        raise Failure("place: un stockage de files.places")
    path = str(path or "").strip("/")
    if ".." in path.split("/"):
        raise Failure("Chemin invalide")
    return f"{place}:{path}"


def places(args):
    return {"places": [{"place": name, "name": name, "kind": conf.get("type", "")} for name, conf in sorted(remotes().items())]}


def entry(e, base=""):
    return {"name": e.get("Name", ""), "path": f"{base.strip('/')}/{e.get('Path', '')}".strip("/"),
            "dir": e.get("IsDir", False), "size": e.get("Size", -1), "modified": e.get("ModTime", "")[:19]}


def listing(args):
    limit = max(1, min(200, int(args.get("limit") or 50)))
    got = json.loads(rclone("lsjson", target(args.get("place"), args.get("path"))) or "[]")
    got.sort(key=lambda e: (not e.get("IsDir"), e.get("Name", "").lower()))
    return {"entries": [entry(e, str(args.get("path") or "")) for e in got[:limit]], "more": len(got) > limit}


def search(args):
    words = [w for w in str(args.get("query") or "").split() if w]
    if not words:
        raise Failure("Donne des mots du nom du fichier")
    limit = max(1, min(50, int(args.get("limit") or 20)))
    names = [args["place"]] if args.get("place") else sorted(remotes())
    results, errors = [], []
    pattern = "*" + "*".join(re.sub(r"[\[\]{}*?\\]", "", w) for w in words) + "*"
    for name in names:
        try:
            got = json.loads(rclone("lsjson", target(name), "--recursive", "--files-only", "--max-depth", "8",
                                    "--ignore-case", "--include", pattern) or "[]")
        except Failure as e:
            errors.append(f"{name}: {e}")
            continue
        results += [{"place": name, "path": e.get("Path", ""), "size": e.get("Size", -1),
                     "modified": e.get("ModTime", "")[:19]} for e in got]
    results.sort(key=lambda r: r["modified"], reverse=True)
    return {"results": results[:limit], **({"errors": errors} if errors else {})}


def download(args):
    src = target(args.get("place"), args.get("path"))
    if not str(args.get("path") or "").strip("/"):
        raise Failure("path: un fichier de files.list ou files.search")
    os.makedirs(DOWNLOADS, exist_ok=True)
    rclone("copy", src, DOWNLOADS, timeout=1800)
    return {"downloaded": os.path.join(DOWNLOADS, os.path.basename(str(args["path"]).rstrip("/")))}


def upload(args):
    local = str(args.get("file") or "")
    if not os.path.isabs(local) or not os.path.isfile(local):
        raise Failure("file: le chemin absolu d'un fichier local")
    rclone("copy", local, target(args.get("place"), args.get("to")), timeout=1800)
    return {"uploaded": os.path.basename(local), "to": target(args.get("place"), args.get("to"))}


S, I = {"type": "string"}, {"type": "integer"}
TOOLS = {
    "places": (places, "The rclone remotes configured.", {"type": "object", "properties": {}}),
    "list": (listing, "A folder's contents.",
             {"type": "object", "required": ["place"], "properties": {"place": S, "path": S, "limit": I}}),
    "search": (search, "Files whose name contains the words.",
               {"type": "object", "required": ["query"], "properties": {"query": S, "place": S, "limit": I}}),
    "download": (download, "Copy a file to ~/Downloads.",
                 {"type": "object", "required": ["place", "path"], "properties": {"place": S, "path": S}}),
    "upload": (upload, "Send a local file to a folder.",
               {"type": "object", "required": ["place", "file"], "properties": {"place": S, "file": S, "to": S}}),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, OSError):
        return {"content": [{"type": "text", "text": "Réponse de rclone inattendue ou argument invalide"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out, "isError": False}


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None  # notification
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "samantha-rclone", "version": "0.1.0"}}
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
