"""python3 -m unittest discover contrib/system"""

import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import server


def write(root, path, text):
    os.makedirs(os.path.dirname(os.path.join(root, path)), exist_ok=True)
    with open(os.path.join(root, path), "w") as f:
        f.write(text)


def stat_line(pid, name, utime, rss_pages, ppid=1, start=100):
    fields = ["S", str(ppid)] + ["0"] * 9 + [str(utime), "0"] + ["0"] * 6 + [str(start), "0", str(rss_pages)]
    return f"{pid} ({name}) " + " ".join(fields) + "\n"


def call(name, arguments):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    return r["result"]


class System(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.proc, self.sys = os.path.join(d.name, "proc"), os.path.join(d.name, "sys")
        write(self.proc, "meminfo", "MemTotal: 16000000 kB\nMemAvailable: 4000000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
        write(self.proc, "loadavg", "3.10 2.00 1.00 2/900 1234\n")
        write(self.proc, "uptime", "7200.5 100.0\n")
        write(self.proc, "42/stat", stat_line(42, "fire fox", 100, 25600))
        write(self.proc, "42/cgroup", "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-firefox.scope\n")
        write(self.proc, "42/cmdline", "firefox\0--private\0")
        write(self.proc, "net/tcp", "  sl local rem st tx rx tr tm retr uid timeout inode\n"
                                    "   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 777\n")
        write(self.proc, "net/tcp6", "header\n   0: 00000000000000000000000000000000:0016 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 888\n")
        os.makedirs(os.path.join(self.proc, "42", "fd"))
        os.symlink("socket:[777]", os.path.join(self.proc, "42", "fd", "3"))
        write(self.sys, "class/hwmon/hwmon0/name", "coretemp\n")
        write(self.sys, "class/hwmon/hwmon0/temp1_input", "58000\n")
        write(self.sys, "class/hwmon/hwmon0/temp1_label", "Package id 0\n")
        for p in (mock.patch.object(server, "PROC", self.proc), mock.patch.object(server, "SYS", self.sys),
                  mock.patch.object(server, "PAGE", 4096), mock.patch.object(server, "TICK", 100),
                  mock.patch("time.sleep")):
            p.start()
            self.addCleanup(p.stop)

    def test_overview(self):
        r = call("overview", {})["structuredContent"]
        self.assertEqual(r["memory"]["used_percent"], 75)
        self.assertEqual(r["load"], [3.1, 2.0, 1.0])
        self.assertEqual(r["temperatures"], [{"sensor": "coretemp Package id 0", "celsius": 58}])
        self.assertEqual(r["uptime_hours"], 2.0)

    def test_processes_name_with_spaces_and_unit(self):
        ticks = iter([100, 150])
        real = server.stat
        with mock.patch.object(server, "stat", lambda pid: (lambda s: (s[0], next(ticks), *s[2:]) if s else s)(real(pid))):
            r = call("processes", {})["structuredContent"]["processes"]
        self.assertEqual(r, [{"pid": 42, "name": "fire fox", "cpu_percent": 100.0, "memory_mib": 100,
                              "unit": "app-firefox.scope"}])

    def test_ports_and_process(self):
        rows = call("ports", {})["structuredContent"]["ports"]
        self.assertEqual([(r["address"], r["port"], r["process"], r["local_only"]) for r in rows],
                         [("::", 22, "", False), ("127.0.0.1", 8080, "fire fox", True)])
        p = call("process", {"pid": 42})["structuredContent"]
        self.assertEqual((p["command"], p["ports"][0]["port"]), ("firefox --private", 8080))
        self.assertTrue(call("process", {"pid": "1; rm"})["isError"])

    def test_stop_validates(self):
        for bad in ({"pid": 1}, {"pid": "42"}, {}):
            self.assertTrue(call("stop", bad)["isError"])
        with mock.patch("os.kill") as kill:
            call("stop", {"pid": 42, "force": True})
        kill.assert_called_once_with(42, server.signal.SIGKILL)

    def test_updates_and_errors(self):
        status = {"deployments": [{"staged": True, "booted": False, "timestamp": 0, "version": "2"},
                                  {"booted": True, "container-image-reference": "img", "timestamp": 0, "version": "1"}]}
        journal = "\n".join(json.dumps({"MESSAGE": "disk error", "_SYSTEMD_UNIT": "x.service",
                                        "__REALTIME_TIMESTAMP": str(1791100000000000 + i)}) for i in range(3))

        def fake(argv, **kw):
            out = json.dumps(status) if argv[0] == "rpm-ostree" else journal if argv[0] == "journalctl" else \
                "foo.service loaded failed failed Foo daemon\n"
            return subprocess.CompletedProcess(argv, 0, out, "")
        with mock.patch("subprocess.run", fake):
            u = call("updates", {})["structuredContent"]
            e = call("errors", {})["structuredContent"]["errors"]
            f = call("services", {})["structuredContent"]["failed"]
        self.assertTrue(u["reboot_needed"])
        self.assertEqual(u["booted"]["version"], "1")
        self.assertEqual((len(e), e[0]["count"]), (1, 3))
        self.assertEqual(f[0], {"unit": "foo.service", "scope": "system", "description": "Foo daemon"})

    def test_protocol(self):
        tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([t["name"] for t in tools],
                         ["overview", "processes", "process", "ports", "stop", "services", "errors", "updates"])


if __name__ == "__main__":
    unittest.main()
