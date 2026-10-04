"""python3 -m unittest discover contrib/vpn"""

import subprocess
import unittest
from unittest import mock

import server

SHOW = ("Freebox:aaaa-1:802-11-wireless:yes:activated\n"
        "Proton FR\\:Paris:bbbb-2:vpn:no:\n"
        "home-wg:cccc-3:wireguard:yes:activated\n")


class Nm:
    def __init__(self, show=SHOW, fail=None):
        self.calls, self.show, self.fail = [], show, fail

    def __call__(self, argv, capture_output, text, timeout, env):
        assert timeout and env["LC_ALL"] == "C"
        self.calls.append(argv[1:])
        if self.fail:
            return subprocess.CompletedProcess(argv, 4, "", self.fail)
        if argv[-2:] == ["connection", "show"]:
            return subprocess.CompletedProcess(argv, 0, self.show, "")
        if "IP4.ADDRESS,GENERAL.STATE" in argv:
            return subprocess.CompletedProcess(argv, 0, "IP4.ADDRESS[1]:10.2.0.2/32\nGENERAL.STATE:activated\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def call(name, arguments):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    return r["result"]


class Vpn(unittest.TestCase):
    def test_status_unescapes_and_filters(self):
        with mock.patch("subprocess.run", Nm()):
            r = call("status", {})["structuredContent"]
        self.assertTrue(r["connected"])
        self.assertEqual([(v["name"], v["active"]) for v in r["vpns"]], [("Proton FR:Paris", False), ("home-wg", True)])
        self.assertEqual(r["vpns"][1]["address"], "10.2.0.2/32")

    def test_connect_and_disconnect_defaults(self):
        nm = Nm()
        with mock.patch("subprocess.run", nm):
            self.assertEqual(call("connect", {})["structuredContent"]["connected"], "Proton FR:Paris")
            self.assertEqual(call("disconnect", {})["structuredContent"]["disconnected"], "home-wg")
            self.assertTrue(call("connect", {"name": "nordvpn"})["isError"])
        self.assertIn(["connection", "up", "uuid", "bbbb-2"], nm.calls)
        self.assertIn(["connection", "down", "uuid", "cccc-3"], nm.calls)

    def test_errors(self):
        with mock.patch("subprocess.run", Nm(show="Freebox:a:802-11-wireless:yes:activated\n")):
            self.assertIn("Aucun VPN configuré", call("connect", {})["content"][0]["text"])
        with mock.patch("subprocess.run", Nm(fail="Error: Connection activation failed: Secrets were required, but not provided")):
            self.assertIn("mot de passe", call("status", {})["content"][0]["text"])

    def test_protocol(self):
        tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["status", "connect", "disconnect"])


if __name__ == "__main__":
    unittest.main()
