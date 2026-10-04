#!/usr/bin/env python3
"""MCP stdio server of the `power` extension (stdlib only).

Battery (UPower), power profiles (power-profiles-daemon), screen brightness
(brightnessctl), and the session: lock, suspend or hibernate now or in some minutes
(logind). D-Bus through `busctl --system`. A delayed suspend lives in this server: a
restart forgets it (`cancel` too). Failures are `isError` results; nothing is logged.
"""

import json
import subprocess
import sys
import threading
import time

PROTOCOL = "2025-06-18"
TIMEOUT = 10
UPOWER = ["org.freedesktop.UPower", "/org/freedesktop/UPower/devices/DisplayDevice"]
PROFILES = ["org.freedesktop.UPower.PowerProfiles", "/org/freedesktop/UPower/PowerProfiles",
            "org.freedesktop.UPower.PowerProfiles"]
LOGIN = ["org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager"]
STATES = {1: "charging", 2: "discharging", 3: "empty", 4: "full", 5: "not charging", 6: "discharging"}
ACTIONS = {"suspend": "Suspend", "hibernate": "Hibernate", "suspend-then-hibernate": "SuspendThenHibernate"}

_lock = threading.Lock()
_pending = {"timer": None, "action": None, "at": None}


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def run(*argv):
    try:
        r = subprocess.run(list(argv), capture_output=True, text=True, timeout=TIMEOUT)
    except FileNotFoundError:
        raise Failure(f"{argv[0]} introuvable")
    except subprocess.TimeoutExpired:
        raise Failure(f"{argv[0]} ne répond pas")
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()
        if "Access denied" in err or "not authorized" in err.lower():
            raise Failure("Le système refuse cette action à Samantha (droits polkit)")
        raise Failure(f"{argv[0]}: {err[:200] or 'échec'}")
    return r.stdout


def bus(*args):
    out = run("busctl", "--system", "--json=short", *args)
    try:
        return json.loads(out)["data"] if out.strip() else None
    except ValueError:
        raise Failure("Réponse D-Bus illisible")


def unwrap(v):
    return v.get("data") if isinstance(v, dict) and "type" in v else v


def battery(args):
    data = bus("call", *UPOWER, "org.freedesktop.DBus.Properties", "GetAll", "s", "org.freedesktop.UPower.Device")
    props = {k: unwrap(v) for k, v in (data[0] if isinstance(data, list) else data).items()}
    if not props.get("IsPresent"):
        return {"present": False}
    state = STATES.get(props.get("State"), "unknown")
    minutes = lambda s: round(s / 60) if isinstance(s, int) and s > 0 else None
    return {"present": True, "percent": round(props.get("Percentage", 0)), "state": state,
            "minutes_to_empty": minutes(props.get("TimeToEmpty")) if state == "discharging" else None,
            "minutes_to_full": minutes(props.get("TimeToFull")) if state == "charging" else None}


def profile(args):
    want = args.get("set")
    if want is not None:
        names = [unwrap(p.get("Profile")) for p in bus("get-property", *PROFILES, "Profiles") or []]
        if want not in names:
            raise Failure(f"Profil inconnu: {want!r} ({', '.join(names)})")
        run("busctl", "--system", "set-property", *PROFILES, "ActiveProfile", "s", want)
    names = [unwrap(p.get("Profile")) for p in bus("get-property", *PROFILES, "Profiles") or []]
    return {"active": bus("get-property", *PROFILES, "ActiveProfile"), "available": names}


def brightness(args):
    if args.get("level") is not None or args.get("change") is not None:
        try:
            if args.get("level") is not None:
                target = f"{max(1, min(100, int(args['level'])))}%"
            else:
                change = int(args["change"])
                target = f"{abs(change)}%{'+' if change >= 0 else '-'}"
        except (TypeError, ValueError):
            raise Failure("Niveau invalide: 1 à 100, ou change +10 / -10")
        run("brightnessctl", "-q", "set", target)
    # `device,class,current,percent,max`
    fields = run("brightnessctl", "-m", "info").strip().split("\n")[0].split(",")
    return {"device": fields[0], "percent": int(fields[3].rstrip("%"))}


def session_now(action):
    if action == "lock":
        # The user's own graphical session (locking all sessions needs an admin).
        try:
            sessions = json.loads(run("loginctl", "list-sessions", "--json=short"))
        except ValueError:
            raise Failure("Sessions illisibles (loginctl)")
        mine = [x for x in sessions if x.get("seat") and x.get("class") == "user"]
        if not mine:
            raise Failure("Aucune session graphique à verrouiller")
        run("loginctl", "lock-session", str(mine[0]["session"]))
        return
    can = bus("call", *LOGIN, "Can" + ACTIONS[action])
    if can not in ("yes", "challenge"):
        raise Failure(f"{action} n'est pas disponible sur cette machine ({can})")
    run("busctl", "--system", "call", *LOGIN, ACTIONS[action], "b", "false")


def fire(action):
    with _lock:
        _pending.update(timer=None, action=None, at=None)
    try:
        session_now(action)
    except Failure:
        pass


def session(args):
    action = args.get("action")
    if action not in ("lock", *ACTIONS):
        raise Failure("action: lock, suspend, hibernate ou suspend-then-hibernate")
    minutes = args.get("in_minutes")
    if not minutes:
        session_now(action)
        return {"done": action}
    try:
        delay = float(minutes) * 60
    except (TypeError, ValueError):
        raise Failure("in_minutes: un nombre de minutes")
    if not 0 < delay <= 24 * 3600:
        raise Failure("in_minutes: entre 1 minute et 24 heures")
    if action != "lock":
        can = bus("call", *LOGIN, "Can" + ACTIONS[action])
        if can not in ("yes", "challenge"):
            raise Failure(f"{action} n'est pas disponible sur cette machine ({can})")
    with _lock:
        if _pending["timer"]:
            _pending["timer"].cancel()
        t = threading.Timer(delay, fire, args=(action,))
        t.daemon = True
        at = time.strftime("%H:%M", time.localtime(time.time() + delay))
        _pending.update(timer=t, action=action, at=at)
        t.start()
    return {"scheduled": action, "at": at}


def cancel(args):
    with _lock:
        t, action = _pending["timer"], _pending["action"]
        if t:
            t.cancel()
        _pending.update(timer=None, action=None, at=None)
    return {"cancelled": action or ""}


# --- MCP ---------------------------------------------------------------------------

S, N = {"type": "string"}, {"type": "number"}
TOOLS = {
    "battery": (battery, {
        "description": "Battery level, charging state and time left.",
        "inputSchema": {"type": "object", "properties": {}},
    }),
    "profile": (profile, {
        "description": "The power profile (power-saver, balanced, performance); `set` changes it.",
        "inputSchema": {"type": "object", "properties": {"set": S}},
    }),
    "brightness": (brightness, {
        "description": "Screen brightness: read it, set a level (1-100) or change it (+10, -10).",
        "inputSchema": {"type": "object", "properties": {"level": N, "change": N}},
    }),
    "session": (session, {
        "description": "Lock the screen, suspend or hibernate, now or in_minutes from now (one "
                       "pending at a time; `cancel` drops it).",
        "inputSchema": {"type": "object", "required": ["action"], "properties": {
            "action": {"type": "string", "enum": ["lock", "suspend", "hibernate", "suspend-then-hibernate"]},
            "in_minutes": N}},
    }),
    "cancel": (cancel, {
        "description": "Cancel a pending delayed lock or suspend.",
        "inputSchema": {"type": "object", "properties": {}},
    }),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Réponse du système inattendue ou "
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
                  "serverInfo": {"name": "samantha-power", "version": "0.1.0"}}
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
