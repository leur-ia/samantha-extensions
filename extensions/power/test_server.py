"""python3 -m unittest discover contrib/power"""

import json
import subprocess
import unittest
from unittest import mock

import server

BATTERY = {"type": "a{sv}", "data": [{"IsPresent": {"type": "b", "data": True}, "Percentage": {"type": "d", "data": 41.6},
                                      "State": {"type": "u", "data": 2}, "TimeToEmpty": {"type": "x", "data": 7260},
                                      "TimeToFull": {"type": "x", "data": 0}}]}
PROFILES = {"type": "aa{sv}", "data": [{"Profile": {"type": "s", "data": p}} for p in ("power-saver", "balanced", "performance")]}


class System:
    def __init__(self, can="yes"):
        self.calls, self.active, self.can = [], "balanced", can

    def __call__(self, argv, capture_output, text, timeout):
        assert timeout
        self.calls.append(argv)
        out = ""
        if argv[:1] == ["busctl"]:
            if "GetAll" in argv:
                out = json.dumps(BATTERY)
            elif argv[-1] == "Profiles":
                out = json.dumps(PROFILES)
            elif argv[-1] == "ActiveProfile" and "get-property" in argv:
                out = json.dumps({"type": "s", "data": self.active})
            elif "set-property" in argv:
                self.active = argv[-1]
            elif argv[-1].startswith("Can"):
                out = json.dumps({"type": "s", "data": self.can})
        elif argv[:2] == ["loginctl", "list-sessions"]:
            out = json.dumps([{"session": "16", "class": "manager", "seat": None},
                              {"session": "15", "class": "user", "seat": "seat0"}])
        elif argv[:2] == ["brightnessctl", "-m"]:
            out = "intel_backlight,backlight,29472,31%,96000\n"
        return subprocess.CompletedProcess(argv, 0, out, "")


def call(name, arguments):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    return r["result"]


class Power(unittest.TestCase):
    def setUp(self):
        self.sys = System()
        p = mock.patch("subprocess.run", self.sys)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(lambda: server.cancel({}))

    def test_battery(self):
        self.assertEqual(call("battery", {})["structuredContent"],
                         {"present": True, "percent": 42, "state": "discharging", "minutes_to_empty": 121, "minutes_to_full": None})

    def test_profile(self):
        self.assertEqual(call("profile", {"set": "power-saver"})["structuredContent"]["active"], "power-saver")
        self.assertTrue(call("profile", {"set": "turbo"})["isError"])

    def test_brightness(self):
        call("brightness", {"change": -10})
        self.assertIn(["brightnessctl", "-q", "set", "10%-"], self.sys.calls)
        call("brightness", {"level": 0})
        self.assertIn(["brightnessctl", "-q", "set", "1%"], self.sys.calls)
        self.assertEqual(call("brightness", {})["structuredContent"]["percent"], 31)

    def test_session_now_later_and_unavailable(self):
        call("session", {"action": "suspend"})
        self.assertIn(["busctl", "--system", "call", *server.LOGIN, "Suspend", "b", "false"], self.sys.calls)
        r = call("session", {"action": "suspend", "in_minutes": 30})["structuredContent"]
        self.assertEqual(r["scheduled"], "suspend")
        self.assertEqual(call("cancel", {})["structuredContent"]["cancelled"], "suspend")
        self.sys.can = "na"
        self.assertIn("pas disponible", call("session", {"action": "hibernate"})["content"][0]["text"])
        self.assertTrue(call("session", {"action": "reboot"})["isError"])
        call("session", {"action": "lock"})
        self.assertIn(["loginctl", "lock-session", "15"], self.sys.calls)

    def test_polkit_refusal_is_a_sentence(self):
        def denied(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, "", "Failed to set property: Access denied")
        with mock.patch("subprocess.run", denied):
            self.assertIn("polkit", call("profile", {"set": "balanced"})["content"][0]["text"])

    def test_protocol(self):
        tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["battery", "profile", "brightness", "session", "cancel"])


if __name__ == "__main__":
    unittest.main()
