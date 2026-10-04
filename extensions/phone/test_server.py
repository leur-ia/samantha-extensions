"""python3 -m unittest discover contrib/phone"""

import json
import subprocess
import unittest
from unittest import mock

import server

LIST = "- Pixel 8: a1b2c3d4_e5f6 (paired and reachable)\n- Tablette: 99ff88ee (paired)\n- Inconnu: 77aa (reachable)\n3 devices found\n"


class Kde:
    def __init__(self, listing=LIST):
        self.calls, self.listing = [], listing

    def __call__(self, argv, capture_output, text, timeout, env):
        assert timeout and env["LC_ALL"] == "C"
        self.calls.append(argv)
        out = ""
        if argv[:2] == ["kdeconnect-cli", "-l"]:
            out = self.listing
        elif "--list-notifications" in argv:
            out = "- WhatsApp: Ana: on se voit à 8h ?\n- Gmail: Facture EDF\n"
        elif argv[0] == "busctl":
            out = json.dumps({"type": "i", "data": 64} if argv[-1] == "charge" else {"type": "b", "data": True})
        return subprocess.CompletedProcess(argv, 0, out, "")


def call(name, arguments):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    return r["result"]


class Phone(unittest.TestCase):
    def setUp(self):
        self.kde = Kde()
        p = mock.patch("subprocess.run", self.kde)
        p.start()
        self.addCleanup(p.stop)

    def test_devices(self):
        self.assertEqual(call("devices", {})["structuredContent"]["devices"], [
            {"id": "a1b2c3d4_e5f6", "name": "Pixel 8", "paired": True, "reachable": True},
            {"id": "99ff88ee", "name": "Tablette", "paired": True, "reachable": False},
            {"id": "77aa", "name": "Inconnu", "paired": False, "reachable": True}])

    def test_default_device_is_the_reachable_paired_one(self):
        self.assertEqual(call("ring", {})["structuredContent"]["ringing"], "Pixel 8")
        self.assertIn(["kdeconnect-cli", "--ring", "-d", "a1b2c3d4_e5f6"], self.kde.calls)
        r = call("ring", {"device": "tablette"})
        self.assertIn("pas joignable", r["content"][0]["text"])

    def test_battery_and_notifications(self):
        self.assertEqual(call("battery", {})["structuredContent"], {"device": "Pixel 8", "charge": 64, "charging": True})
        n = call("notifications", {})["structuredContent"]["notifications"]
        self.assertEqual(n[0], {"app": "WhatsApp", "text": "Ana: on se voit à 8h ?"})

    def test_sms_validation(self):
        call("send_sms", {"to": "+33 6 12 34 56 78", "text": "J'arrive"})
        self.assertIn(["kdeconnect-cli", "--send-sms", "J'arrive", "--destination", "+33612345678", "-d", "a1b2c3d4_e5f6"], self.kde.calls)
        for bad in ({"to": "maman", "text": "x"}, {"to": "0612345678", "text": " "}, {"to": "06; rm", "text": "x"}):
            self.assertTrue(call("send_sms", bad)["isError"])

    def test_share_needs_absolute_path_or_link(self):
        self.assertTrue(call("share", {"what": "photo.jpg"})["isError"])
        self.assertFalse(call("share", {"what": "https://example.org"})["isError"])

    def test_pair_and_no_phone(self):
        self.assertEqual(call("pair", {})["structuredContent"]["requested"], "Inconnu")
        self.kde.listing = "0 devices found\n"
        self.assertIn("KDE Connect", call("battery", {})["content"][0]["text"])

    def test_protocol(self):
        tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([t["name"] for t in tools],
                         ["devices", "battery", "ring", "notifications", "send_sms", "share", "message", "pair"])


if __name__ == "__main__":
    unittest.main()
