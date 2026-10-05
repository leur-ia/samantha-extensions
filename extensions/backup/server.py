#!/usr/bin/env python3
"""MCP stdio server of the `backup` extension (stdlib only).

Backups with restic (in the image). ~/.config/samantha/backup.toml:

    repository = "/run/media/jb/Disque/restic"   # or sftp:host:/path, rest:https://…
    paths = ["~/Documents", "~/Images", "~/.config"]
    exclude = ["*.tmp", "~/.cache"]
    keep_daily = 7                               # optional: forget older snapshots
    keep_weekly = 4

The repository password is the Secret Service item `backup-restic` (RESTIC_PASSWORD).
A backup runs in the background: an island activity (`backup.activity`) while it
runs, a `backup.done` event at the end (watchers: a nightly backup). Restores go to
~/Restauré/<date>/, never over the user's files. Nothing is logged.
"""

import datetime
import json
import os
import re
import socket
import subprocess
import sys
import threading
import tomllib

PROTOCOL = "2025-06-18"
TIMEOUT = 120
RESTORED = os.path.expanduser("~/Restauré")
SNAPSHOT = re.compile(r"^(latest|[0-9a-f]{8,64})$")
SETUP = ("La sauvegarde n'est pas configurée: écris ~/.config/samantha/backup.toml (repository, "
         "paths), enregistre le mot de passe du dépôt avec `samantha provider set-key backup-restic`, "
         "puis crée le dépôt (backup.init).")

_lock = threading.Lock()
_running = {"since": None, "last": None}


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def config():
    home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    try:
        with open(os.path.join(home, "samantha", "backup.toml"), "rb") as f:
            c = tomllib.load(f)
    except FileNotFoundError:
        raise Failure(SETUP)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise Failure(f"backup.toml illisible: {e}")
    if not isinstance(c.get("repository"), str) or not c["repository"]:
        raise Failure(SETUP)
    if not os.environ.get("RESTIC_PASSWORD"):
        raise Failure(SETUP)
    c["paths"] = [os.path.expanduser(p) for p in c.get("paths") or [] if isinstance(p, str)]
    c["exclude"] = [os.path.expanduser(p) for p in c.get("exclude") or [] if isinstance(p, str)]
    return c


def restic(c, *args, timeout=TIMEOUT):
    env = {**os.environ, "RESTIC_REPOSITORY": c["repository"], "RESTIC_PROGRESS_FPS": "0.2"}
    try:
        r = subprocess.run(["restic", *args], capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        raise Failure("restic n'est pas installé")
    except subprocess.TimeoutExpired:
        raise Failure("restic ne répond pas (dépôt injoignable ?)")
    if r.returncode != 0:
        err = r.stderr.strip()
        if "wrong password" in err or "no key found" in err:
            raise Failure("Mot de passe du dépôt refusé (backup-restic)")
        if "Is there a repository" in err or "unable to open config file" in err:
            raise Failure("Pas de dépôt à cet endroit (disque branché ? sinon backup.init le crée)")
        raise Failure(f"restic: {err.splitlines()[-1] if err else 'échec'}"[:300])
    return r.stdout


def daemon_publish(kind, data):
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


def snapshot_row(s):
    return {"id": s.get("short_id") or s.get("id", "")[:8],
            "time": s.get("time", "")[:16].replace("T", " "), "paths": s.get("paths", []),
            "host": s.get("hostname", ""), "tags": s.get("tags") or []}


def snapshots(args):
    c = config()
    got = json.loads(restic(c, "snapshots", "--json") or "[]")
    limit = max(1, min(50, int(args.get("limit") or 10)))
    return {"snapshots": [snapshot_row(s) for s in reversed(got)][:limit]}


def status(args):
    c = config()
    with _lock:
        running = _running["since"]
        last = _running["last"]
    out = {"repository": c["repository"], "paths": c["paths"], "running_since": running, "last_run": last}
    try:
        latest = json.loads(restic(c, "snapshots", "--json", "--latest", "1") or "[]")
        out["latest"] = snapshot_row(latest[-1]) if latest else None
    except Failure as e:
        out["error"] = str(e)
    return out


def run_backup(c):
    started = datetime.datetime.now().strftime("%H:%M")
    daemon_publish("backup.activity", {"key": "run", "text": "Sauvegarde en cours", "sub": f"depuis {started}"})
    result = {"ok": False}
    try:
        args = ["backup", "--json", *c["paths"]]
        for e in c["exclude"]:
            args += ["--exclude", e]
        out = restic(c, *args, timeout=6 * 3600)
        summary = next((json.loads(l) for l in reversed(out.splitlines()) if '"summary"' in l), {})
        result = {"ok": True, "snapshot": summary.get("snapshot_id", "")[:8],
                  "files_new": summary.get("files_new", 0), "files_changed": summary.get("files_changed", 0),
                  "added_mb": round(summary.get("data_added", 0) / 1e6, 1)}
        keep = [f"--keep-{k.split('_', 1)[1]}={c[k]}" for k in ("keep_daily", "keep_weekly", "keep_monthly")
                if isinstance(c.get(k), int)]
        if keep:
            restic(c, "forget", "--prune", *keep, timeout=3600)
    except Failure as e:
        result = {"ok": False, "error": str(e)}
    finally:
        with _lock:
            _running["since"] = None
            _running["last"] = {"at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), **result}
        text = "Sauvegarde terminée" if result["ok"] else "Sauvegarde échouée"
        daemon_publish("backup.activity", {"key": "run", "text": text, "icon": "check" if result["ok"] else "error",
                                           "sub": result.get("error", ""), "ttl_s": 10})
        daemon_publish("backup.done", result)


def backup(args):
    c = config()
    if not c["paths"]:
        raise Failure("Aucun dossier à sauvegarder (paths dans backup.toml)")
    with _lock:
        if _running["since"]:
            return {"running_since": _running["since"]}
        _running["since"] = datetime.datetime.now().strftime("%H:%M")
    threading.Thread(target=run_backup, args=(c,), daemon=True).start()
    return {"started": True, "paths": c["paths"]}


def init(args):
    c = config()
    restic(c, "init")
    return {"initialized": c["repository"]}


def check_snapshot(args):
    sid = str(args.get("snapshot") or "latest")
    if not SNAPSHOT.match(sid):
        raise Failure("snapshot: un id de backup.snapshots, ou latest")
    return sid


def files(args):
    c = config()
    sid = check_snapshot(args)
    path = os.path.expanduser(str(args.get("path") or "/"))
    out = restic(c, "ls", "--json", sid, path)
    rows = []
    for line in out.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("struct_type") == "node" or "path" in e and "type" in e:
            rows.append({"path": e.get("path", ""), "type": e.get("type", ""), "size": e.get("size", 0)})
    limit = max(1, min(200, int(args.get("limit") or 50)))
    return {"snapshot": sid, "entries": rows[:limit], "more": len(rows) > limit}


def restore(args):
    c = config()
    sid = check_snapshot(args)
    path = os.path.expanduser(str(args.get("path") or ""))
    if not path.startswith("/") or ".." in path.split("/"):
        raise Failure("path: un chemin absolu dans la sauvegarde (de backup.files)")
    target = os.path.join(RESTORED, datetime.datetime.now().strftime("%Y-%m-%d_%H%M"))
    restic(c, "restore", sid, "--target", target, "--include", path, timeout=6 * 3600)
    return {"restored": path, "into": target}


S, I = {"type": "string"}, {"type": "integer"}
TOOLS = {
    "status": (status, "Backup configuration, latest snapshot, a backup running now, the last run's result.",
               {"type": "object", "properties": {}}),
    "snapshots": (snapshots, "Snapshots in the repository, newest first.",
                  {"type": "object", "properties": {"limit": I}}),
    "backup": (backup, "Start a backup now (in the background; the island shows it).",
               {"type": "object", "properties": {}}),
    "files": (files, "Files in a snapshot (latest by default) under a path.",
              {"type": "object", "properties": {"snapshot": S, "path": S, "limit": I}}),
    "restore": (restore, "Restore a file or folder from a snapshot into ~/Restauré/<date>/ (never over the original).",
                {"type": "object", "required": ["path"], "properties": {"snapshot": S, "path": S}}),
    "init": (init, "Create the repository configured in backup.toml.", {"type": "object", "properties": {}}),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Réponse de restic inattendue ou argument invalide"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out, "isError": False}


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None  # notification
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "samantha-backup", "version": "0.1.0"}}
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
