#!/usr/bin/env python3
"""MCP stdio server of the `system` extension (stdlib only).

"Pourquoi ça rame ?": load, memory, disks and temperatures from /proc and /sys; the
busiest processes with the systemd unit that started each; what listens on which
port (/proc/net, no netlink needed); stopping a process (confirmed); failed services;
recent errors from the journal; the system image's update state (rpm-ostree).
Command lines can hold secrets: only `process` shows one. Nothing is logged.
"""

import json
import os
import re
import signal
import subprocess
import sys
import time

PROTOCOL = "2025-06-18"
PROC = "/proc"
SYS = "/sys"
TIMEOUT = 15
TICK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def run(*argv):
    try:
        r = subprocess.run(list(argv), capture_output=True, text=True, timeout=TIMEOUT,
                           env={**os.environ, "LC_ALL": "C"})
    except FileNotFoundError:
        raise Failure(f"{argv[0]} introuvable")
    except subprocess.TimeoutExpired:
        raise Failure(f"{argv[0]} ne répond pas")
    return r


def gib(n):
    return round(n / 2**30, 1)


# --- overview ----------------------------------------------------------------------

def meminfo():
    out = {}
    for line in read(f"{PROC}/meminfo").splitlines():
        k, _, v = line.partition(":")
        if v.strip().split()[:1]:
            out[k] = int(v.split()[0]) * 1024
    return out


def temperatures():
    rows = []
    base = f"{SYS}/class/hwmon"
    for h in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        name = read(f"{base}/{h}/name").strip()
        for f in sorted(os.listdir(f"{base}/{h}")):
            if re.fullmatch(r"temp\d+_input", f):
                v = read(f"{base}/{h}/{f}").strip()
                label = read(f"{base}/{h}/{f.replace('_input', '_label')}").strip()
                if v.lstrip("-").isdigit():
                    rows.append({"sensor": f"{name} {label}".strip(), "celsius": round(int(v) / 1000)})
    return rows[:16]


def disks():
    rows, seen = [], set()
    for path in ("/", "/var", "/boot", "/boot/efi"):
        try:
            st = os.statvfs(path)
        except OSError:
            continue
        key = (st.f_blocks, st.f_bfree)
        # bootc's / is the read-only image (composefs): nothing to fill there.
        if key in seen or not st.f_blocks or st.f_flag & os.ST_RDONLY:
            continue
        seen.add(key)
        total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
        rows.append({"mount": path, "total_gib": gib(total), "free_gib": gib(free),
                     "used_percent": round(100 * (1 - free / total))})
    return rows


def overview(args):
    load = read(f"{PROC}/loadavg").split()[:3]
    m = meminfo()
    up = float((read(f"{PROC}/uptime").split() or ["0"])[0])
    return {
        "cpus": os.cpu_count(), "load": [float(x) for x in load],
        "memory": {"total_gib": gib(m.get("MemTotal", 0)), "available_gib": gib(m.get("MemAvailable", 0)),
                   "used_percent": round(100 * (1 - m.get("MemAvailable", 0) / max(1, m.get("MemTotal", 1)))),
                   "swap_used_gib": gib(m.get("SwapTotal", 0) - m.get("SwapFree", 0))},
        "disks": disks(), "temperatures": temperatures(), "uptime_hours": round(up / 3600, 1),
    }


# --- processes ---------------------------------------------------------------------

def stat(pid):
    """(name, cpu ticks, rss bytes, ppid, start ticks) or None."""
    raw = read(f"{PROC}/{pid}/stat")
    if ")" not in raw:
        return None
    name = raw[raw.index("(") + 1:raw.rindex(")")]
    f = raw[raw.rindex(")") + 2:].split()
    return name, int(f[11]) + int(f[12]), int(f[21]) * PAGE, int(f[1]), int(f[19])


def unit_of(pid):
    """The systemd unit (service, scope) a process runs in, from its cgroup."""
    for line in read(f"{PROC}/{pid}/cgroup").splitlines():
        path = line.split(":", 2)[-1]
        units = [p for p in path.split("/") if p.endswith((".service", ".scope"))]
        if units:
            return units[-1]
    return ""


def pids():
    return [p for p in os.listdir(PROC) if p.isdigit()]


def processes(args):
    sort = args.get("sort") or "cpu"
    if sort not in ("cpu", "memory"):
        raise Failure("sort: cpu ou memory")
    limit = max(1, min(30, int(args.get("limit") or 10)))
    first = {p: stat(p) for p in pids()}
    time.sleep(0.5)
    rows = []
    for p in pids():
        s = stat(p)
        if not s:
            continue
        before = first.get(p)
        cpu = (s[1] - before[1]) / TICK / 0.5 * 100 if before else 0.0
        rows.append({"pid": int(p), "name": s[0], "cpu_percent": round(cpu, 1),
                     "memory_mib": round(s[2] / 2**20), "unit": unit_of(p)})
    key = "cpu_percent" if sort == "cpu" else "memory_mib"
    rows.sort(key=lambda r: -r[key])
    return {"processes": rows[:limit]}


def process(args):
    pid = str(args.get("pid") or "")
    if not pid.isdigit() or not os.path.exists(f"{PROC}/{pid}"):
        raise Failure("Processus introuvable")
    s = stat(pid)
    chain, p = [], s[3]
    while p > 1 and len(chain) < 8:
        ps = stat(str(p))
        if not ps:
            break
        chain.append({"pid": p, "name": ps[0]})
        p = ps[3]
    boot = time.time() - float(read(f"{PROC}/uptime").split()[0])
    started = time.strftime("%Y-%m-%d %H:%M", time.localtime(boot + s[4] / TICK))
    cmd = read(f"{PROC}/{pid}/cmdline").replace("\0", " ").strip()[:500]
    return {"pid": int(pid), "name": s[0], "command": cmd, "unit": unit_of(pid), "started": started,
            "memory_mib": round(s[2] / 2**20), "parents": chain,
            "ports": [r for r in listening() if r["pid"] == int(pid)]}


def stop(args):
    pid = args.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        raise Failure("pid invalide")
    try:
        os.kill(pid, signal.SIGKILL if args.get("force") else signal.SIGTERM)
    except ProcessLookupError:
        raise Failure("Processus introuvable")
    except PermissionError:
        raise Failure("Ce processus n'appartient pas à l'utilisateur: impossible de l'arrêter")
    return {"stopped": pid, "signal": "KILL" if args.get("force") else "TERM"}


# --- ports -------------------------------------------------------------------------

def addr(hexaddr):
    ip, port = hexaddr.split(":")
    if len(ip) == 8:
        host = ".".join(str(int(ip[i:i + 2], 16)) for i in (6, 4, 2, 0))
    else:
        words = [ip[i:i + 8] for i in range(0, 32, 8)]
        raw = "".join("".join(w[j:j + 2] for j in (6, 4, 2, 0)) for w in words)
        host = ":".join(raw[i:i + 4] for i in range(0, 32, 4))
        host = re.sub(r"(^|:)0{1,3}", r"\1", host)
        host = "::" if set(host) <= {"0", ":"} else host
    return host, int(port, 16)


def socket_owners():
    owners = {}
    for p in pids():
        try:
            for fd in os.listdir(f"{PROC}/{p}/fd"):
                link = os.readlink(f"{PROC}/{p}/fd/{fd}")
                if link.startswith("socket:["):
                    owners[link[8:-1]] = int(p)
        except OSError:
            continue  # another user's process
    return owners


def listening():
    owners, rows = socket_owners(), []
    for proto in ("tcp", "tcp6", "udp", "udp6"):
        for line in read(f"{PROC}/net/{proto}").splitlines()[1:]:
            f = line.split()
            if len(f) < 10:
                continue
            # TCP LISTEN is 0A; an unconnected UDP socket (07) is a listener.
            if (proto.startswith("tcp") and f[3] != "0A") or (proto.startswith("udp") and f[3] != "07"):
                continue
            host, port = addr(f[1])
            pid = owners.get(f[9])
            s = stat(str(pid)) if pid else None
            rows.append({"proto": proto[:3], "address": host, "port": port, "pid": pid,
                         "process": s[0] if s else "", "unit": unit_of(pid) if pid else "",
                         "local_only": host in ("127.0.0.1", "::1")})
    rows.sort(key=lambda r: (r["port"], r["proto"]))
    return rows


def ports(args):
    rows = listening()
    if args.get("port"):
        rows = [r for r in rows if r["port"] == int(args["port"])]
    return {"ports": rows[:100]}


# --- services, errors, updates -----------------------------------------------------

def services(args):
    out = []
    for scope in ([], ["--user"]):
        r = run("systemctl", *scope, "--failed", "--no-legend", "--plain", "--no-pager")
        for line in r.stdout.splitlines():
            parts = line.split(None, 4)
            if parts and parts[0].endswith((".service", ".timer", ".socket", ".mount", ".scope")):
                out.append({"unit": parts[0], "scope": "user" if scope else "system",
                            "description": parts[4] if len(parts) > 4 else ""})
    return {"failed": out}


def errors(args):
    minutes = max(1, min(24 * 60, int(args.get("minutes") or 60)))
    r = run("journalctl", "-p", "err", "--since", f"-{minutes}min", "-o", "json", "--no-pager", "-n", "200")
    if r.returncode != 0 and not r.stdout:
        raise Failure("Journal illisible (l'utilisateur doit être dans le groupe wheel ou adm)")
    rows = []
    for line in r.stdout.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        msg = e.get("MESSAGE")
        if isinstance(msg, list):  # binary messages come as byte arrays
            msg = bytes(msg).decode("utf-8", "replace")
        t = int(e.get("__REALTIME_TIMESTAMP", "0")) / 1e6
        rows.append({"time": time.strftime("%H:%M:%S", time.localtime(t)),
                     "source": e.get("_SYSTEMD_UNIT") or e.get("SYSLOG_IDENTIFIER") or e.get("_COMM", ""),
                     "message": str(msg)[:300]})
    # The same error repeated is one row with a count.
    grouped = {}
    for row in rows:
        k = (row["source"], row["message"])
        g = grouped.setdefault(k, {**row, "count": 0})
        g["count"] += 1
        g["time"] = row["time"]
    return {"errors": sorted(grouped.values(), key=lambda g: g["time"], reverse=True)[:40]}


def updates(args):
    r = run("rpm-ostree", "status", "--json")
    try:
        deployments = json.loads(r.stdout)["deployments"]
    except (ValueError, KeyError):
        raise Failure("État du système illisible (rpm-ostree)")
    when = lambda d: time.strftime("%Y-%m-%d %H:%M", time.localtime(d.get("timestamp", 0)))
    booted = next((d for d in deployments if d.get("booted")), {})
    staged = next((d for d in deployments if d.get("staged")), None)
    return {"booted": {"image": booted.get("container-image-reference", ""), "built": when(booted),
                       "version": booted.get("version", "")},
            "staged": {"built": when(staged), "version": staged.get("version", "")} if staged else None,
            "reboot_needed": staged is not None}


# --- MCP ---------------------------------------------------------------------------

S, I = {"type": "string"}, {"type": "integer"}
TOOLS = {
    "overview": (overview, {
        "description": "Load average, CPU count, memory and swap, disk space, temperatures, uptime.",
        "inputSchema": {"type": "object", "properties": {}},
    }),
    "processes": (processes, {
        "description": "Busiest processes by cpu (default) or memory, with the systemd unit each runs in.",
        "inputSchema": {"type": "object", "properties": {
            "sort": {"type": "string", "enum": ["cpu", "memory"]}, "limit": I}},
    }),
    "process": (process, {
        "description": "One process: command line, unit, start time, parent chain, listening ports.",
        "inputSchema": {"type": "object", "required": ["pid"], "properties": {"pid": I}},
    }),
    "ports": (ports, {
        "description": "Listening TCP/UDP ports with the process and unit behind each (filter by port).",
        "inputSchema": {"type": "object", "properties": {"port": I}},
    }),
    "stop": (stop, {
        "description": "Stop one of the user's processes (TERM; force: KILL).",
        "inputSchema": {"type": "object", "required": ["pid"], "properties": {
            "pid": I, "force": {"type": "boolean"}}},
    }),
    "services": (services, {
        "description": "Failed systemd units, system and user.",
        "inputSchema": {"type": "object", "properties": {}},
    }),
    "errors": (errors, {
        "description": "Error-level journal messages of the last minutes (default 60), grouped.",
        "inputSchema": {"type": "object", "properties": {"minutes": I}},
    }),
    "updates": (updates, {
        "description": "The system image: booted build, a staged update waiting for a reboot. "
                       "(App updates: the flatpak extension.)",
        "inputSchema": {"type": "object", "properties": {}},
    }),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Lecture du système inattendue ou "
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
                  "serverInfo": {"name": "samantha-system", "version": "0.1.0"}}
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
