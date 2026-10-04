"""python3 -m unittest discover contrib/notifications"""

import json
import os
import stat
import tempfile
import unittest
from unittest import mock

import server

T0 = 1791129300.0


def line(app, summary, body="", t=T0, urgency=None):
    hints = {"urgency": {"type": "y", "data": urgency}} if urgency is not None else {}
    return json.dumps({"type": "method_call", "member": "Notify", "timestamp-realtime": int(t * 1e6),
                       "payload": {"type": "susssasa{sv}i", "data": [app, 0, "", summary, body, [], hints, -1]}})


def call(name, arguments):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    return r["result"]


class Notifications(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.clock = [T0 + 60]
        for p in (mock.patch.object(server, "DIR", d.name),
                  mock.patch.object(server, "HISTORY", os.path.join(d.name, "h.jsonl")),
                  mock.patch.object(server, "SEEN", os.path.join(d.name, "seen")),
                  mock.patch.object(server, "now", lambda: self.clock[0])):
            p.start()
            self.addCleanup(p.stop)
        server._items.clear()

    def test_parse(self):
        n = server.parse(line("Firefox", "Download done", "x" * 900, urgency=2))
        self.assertEqual((n["app"], n["summary"], len(n["body"]), n["urgency"], n["time"]),
                         ("Firefox", "Download done", 500, "critical", T0))
        self.assertIsNone(server.parse("not json"))
        self.assertIsNone(server.parse(json.dumps({"payload": {"data": [1, 2]}})))

    def test_kept_across_restarts_owner_only_and_expiring(self):
        server.add(server.parse(line("Old", "a", t=T0 - 8 * 86400)))
        server.add(server.parse(line("Mail", "b")))
        self.assertEqual(stat.S_IMODE(os.stat(server.HISTORY).st_mode), 0o600)
        server._items.clear()
        server.load()
        self.assertEqual([n["app"] for n in server._items], ["Mail"])

    def test_recent_new_only_and_search(self):
        server.add(server.parse(line("Slack", "Ana: tu viens ?", t=T0 - 3600)))
        server.add(server.parse(line("Mail", "Facture", "Votre facture EDF", t=T0)))
        r = call("recent", {"new_only": True})["structuredContent"]
        self.assertEqual([n["app"] for n in r["notifications"]], ["Mail", "Slack"])
        self.assertEqual(call("recent", {"new_only": True})["structuredContent"]["count"], 0)
        self.clock[0] += 10
        server.add(server.parse(line("Mail", "Nouveau", t=self.clock[0])))
        self.assertEqual(call("recent", {"new_only": True})["structuredContent"]["count"], 1)
        self.assertEqual(call("recent", {"minutes": 30})["structuredContent"]["count"], 2)
        self.assertEqual(call("recent", {"app": "slack"})["structuredContent"]["count"], 1)
        self.assertEqual(call("search", {"query": "edf"})["structuredContent"]["notifications"][0]["summary"], "Facture")
        self.assertTrue(call("search", {"query": " "})["isError"])
        self.assertEqual(call("apps", {})["structuredContent"]["apps"][0], {"app": "Mail", "count": 2})

    def test_clear(self):
        server.add(server.parse(line("Mail", "b")))
        self.assertEqual(call("clear", {})["structuredContent"]["cleared"], 1)
        server.load()
        self.assertEqual(server._items, [])

    def test_protocol(self):
        tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["recent", "search", "apps", "clear"])


if __name__ == "__main__":
    unittest.main()
