#!/usr/bin/env python3
"""MCP stdio server of the `shell` extension (stdlib only).

`shell.run` runs one bash command (the daemon has the user confirm it first) in the
extension's sandbox, with a time limit and its output cut to what a model can read.
`shell.help` reads a tool's manual (man, tldr, then --help); `shell.which` says which
tools are installed. Failures are `isError` results, never a crash.
"""

import json
import os
import shutil
import subprocess
import sys

PROTOCOL = "2025-06-18"
OUTPUT = 12_000
HELP = 6_000
TIMEOUT, MAX_TIMEOUT = 60, 600
# What a day-to-day request usually needs, checked by shell.which without names.
COMMON = ["bash", "git", "curl", "wget", "rsync", "tar", "zip", "unzip", "7z", "jq", "rg", "fd",
          "python3", "node", "npm", "sqlite3", "ffmpeg", "yt-dlp", "pandoc", "magick", "convert",
          "pdftotext", "qpdf", "exiftool", "rclone", "restic", "podman", "toolbox", "distrobox",
          "flatpak", "systemctl", "journalctl", "nmcli", "tailscale", "tldr", "man"]


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def home():
    return os.path.expanduser("~")


def cut(text, cap):
    if len(text) <= cap:
        return text
    half = cap // 2
    return f"{text[:half]}\n… ({len(text) - cap} caractères coupés) …\n{text[-half:]}"


def run(args):
    command = (args.get("command") or "").strip()
    if not command:
        raise Failure("Donne une commande")
    cwd = os.path.expanduser(args.get("cwd") or "~")
    if not os.path.isdir(cwd):
        raise Failure(f"Dossier introuvable ici : {cwd} (seuls les dossiers usuels sont partagés)")
    timeout = max(1, min(int(args.get("timeout") or TIMEOUT), MAX_TIMEOUT))
    try:
        p = subprocess.run(["bash", "-lc", command], cwd=cwd, capture_output=True, text=True,
                           errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                           env={**os.environ, "HOME": home(), "TERM": "dumb", "NO_COLOR": "1",
                                "PAGER": "cat", "GIT_PAGER": "cat"})
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return {"exit": None, "timed_out": True, "seconds": timeout,
                "stdout": cut(out, OUTPUT), "stderr": ""}
    return {"exit": p.returncode, "stdout": cut(p.stdout, OUTPUT), "stderr": cut(p.stderr, OUTPUT // 2)}


def quiet(cmd, timeout=8):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL,
                           env={**os.environ, "MANWIDTH": "100", "MANPAGER": "cat", "PAGER": "cat",
                                "TERM": "dumb", "NO_COLOR": "1"})
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return (p.stdout or "") + ("" if p.stdout.strip() else p.stderr or "")


def help_(args):
    program = (args.get("program") or "").strip()
    if not program or not all(c.isalnum() or c in "-_.+" for c in program):
        raise Failure("Donne le nom d'un programme (sans options)")
    path = shutil.which(program)
    if not path:
        raise Failure(f"{program} n'est pas installé ici")
    for source, cmd in (("man", ["man", "-P", "cat", program]),
                        ("tldr", ["tldr", program]),
                        ("--help", [path, "--help"])):
        if source != "--help" and not shutil.which(cmd[0]):
            continue
        text = quiet(cmd).strip()
        if len(text) > 40:
            return {"program": program, "path": path, "source": source, "text": cut(text, HELP)}
    raise Failure(f"Pas d'aide trouvée pour {program}")


def which(args):
    names = args.get("names") or COMMON
    if not isinstance(names, list):
        raise Failure("names est une liste de programmes")
    found, missing = {}, []
    for n in names:
        p = shutil.which(str(n))
        if p:
            found[str(n)] = p
        else:
            missing.append(str(n))
    return {"found": found, "missing": missing}


def obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


TOOLS = {
    "run": (run, "Runs one bash command, after the user confirms it: {exit, stdout, stderr} "
            "(timed_out when it ran too long). The everyday folders (~/Documents, ~/Downloads, "
            "~/Pictures, ~/Music, ~/Videos, ~/Desktop, ~/src, ~/Projects, ~/otto) are shared, and "
            "the files sent from the app (~/.local/share/otto/uploads, read-only); no sudo.",
            obj({"command": {"type": "string"},
                 "cwd": {"type": "string", "description": "Where to run it (default ~)."},
                 "timeout": {"type": "integer", "description": f"Seconds, default {TIMEOUT}, at most {MAX_TIMEOUT}."}},
                ["command"])),
    "help": (help_, "A program's manual (man, tldr, or its --help), cut to the essentials: "
             "read it before using options you're not sure of.",
             obj({"program": {"type": "string"}}, ["program"])),
    "which": (which, "Which command-line tools are installed: {found: {name: path}, missing}; "
              "without names, the common ones.",
              obj({"names": {"type": "array", "items": {"type": "string"}}})),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
        return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
                "structuredContent": out}
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}


def handle(msg):
    method, params, mid = msg.get("method"), msg.get("params") or {}, msg.get("id")
    if mid is None:
        return None
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "otto-shell", "version": "0.1.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": n, "description": d, "inputSchema": s}
                            for n, (_, d, s) in TOOLS.items()]}
    elif method == "tools/call" and params.get("name") in TOOLS:
        result = call(params["name"], params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


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
