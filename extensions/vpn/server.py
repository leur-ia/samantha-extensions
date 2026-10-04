#!/usr/bin/env python3
"""MCP stdio server of the `vpn` extension (stdlib only).

The VPN connections NetworkManager knows (WireGuard, OpenVPN, Proton, GlobalProtect…
any `vpn` or `wireguard` profile): which are up, connect, disconnect. `nmcli` on the
system bus; polkit lets the user control them. A profile whose secrets aren't saved
can't be started from here: the user connects it once from the network settings with
"save password". Nothing is logged.
"""

import json
import re
import subprocess
import sys

PROTOCOL = "2025-06-18"
TIMEOUT = 45
VPN_TYPES = ("vpn", "wireguard")


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def run(*args):
    try:
        r = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=TIMEOUT,
                           env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
    except FileNotFoundError:
        raise Failure("nmcli introuvable (NetworkManager)")
    except subprocess.TimeoutExpired:
        raise Failure("Le VPN ne répond pas (délai dépassé)")
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()
        if "secrets" in err.lower() or "password" in err.lower():
            raise Failure("Ce VPN demande un mot de passe non enregistré: connecte-le une fois depuis les "
                          "réglages réseau en cochant « enregistrer le mot de passe ».")
        if "Not authorized" in err:
            raise Failure("Le système refuse cette action à Samantha (droits polkit)")
        raise Failure(f"NetworkManager: {err[:200] or 'échec'}")
    return r.stdout


def fields(line):
    """One `nmcli -t` line: fields split on unescaped ':', escapes removed."""
    return [re.sub(r"\\(.)", r"\1", f) for f in re.split(r"(?<!\\):", line)]


def connections():
    rows = []
    for line in run("-t", "-f", "NAME,UUID,TYPE,ACTIVE,STATE", "connection", "show").splitlines():
        f = fields(line)
        if len(f) >= 4 and f[2] in VPN_TYPES:
            rows.append({"name": f[0], "uuid": f[1], "type": f[2],
                         "active": f[3] == "yes", "state": f[4] if len(f) > 4 else ""})
    return rows


def status(args):
    rows = connections()
    for r in rows:
        if r["active"]:
            info = dict(fields(l)[:2] for l in run("-t", "-f", "IP4.ADDRESS,GENERAL.STATE",
                                                    "connection", "show", r["uuid"]).splitlines() if ":" in l)
            r["address"] = next((v for k, v in info.items() if k.startswith("IP4.ADDRESS")), "")
    return {"connected": any(r["active"] for r in rows), "vpns": rows}


def find(ref, want_active=None):
    rows = connections()
    if not rows:
        raise Failure("Aucun VPN configuré: ajoute-le dans les réglages réseau (ou importe un .conf WireGuard).")
    ref = str(ref or "").strip().lower()
    pool = [r for r in rows if want_active is None or r["active"] == want_active] if not ref else rows
    match = [r for r in pool if not ref or ref == r["uuid"] or ref in r["name"].lower()]
    if len(match) != 1:
        names = ", ".join(r["name"] for r in rows)
        raise Failure(("Plusieurs VPN correspondent" if match else "Aucun VPN ne correspond") + f" ({names})")
    return match[0]


def connect(args):
    r = find(args.get("name"), want_active=False)
    run("connection", "up", "uuid", r["uuid"])
    return {"connected": r["name"]}


def disconnect(args):
    r = find(args.get("name"), want_active=True)
    run("connection", "down", "uuid", r["uuid"])
    return {"disconnected": r["name"]}


S = {"type": "string"}
NAME = {"type": "string", "description": "VPN name (fragment); default: the only one that fits."}
TOOLS = {
    "status": (status, {
        "description": "VPN profiles NetworkManager knows, which are connected, with their address.",
        "inputSchema": {"type": "object", "properties": {}},
    }),
    "connect": (connect, {
        "description": "Connect a VPN profile.",
        "inputSchema": {"type": "object", "properties": {"name": NAME}},
    }),
    "disconnect": (disconnect, {
        "description": "Disconnect a VPN (default: the connected one).",
        "inputSchema": {"type": "object", "properties": {"name": NAME}},
    }),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
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
                  "serverInfo": {"name": "samantha-vpn", "version": "0.1.0"}}
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
