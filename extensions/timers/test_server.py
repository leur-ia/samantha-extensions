"""python3 -m unittest discover contrib/timers"""

import datetime
import json
import os
import tempfile
import unittest
from unittest import mock

import server


def call(name, arguments):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    return r["result"]


class Timers(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.clock = [datetime.datetime(2026, 10, 4, 14, 0).timestamp()]
        self.events = []
        for p in (mock.patch.object(server, "STATE", os.path.join(d.name, "t.json")),
                  mock.patch.object(server, "now", lambda: self.clock[0]),
                  mock.patch.object(server, "publish", lambda k, data: self.events.append((k, data))),
                  mock.patch.object(server, "SOUND", "/nonexistent"),
                  mock.patch.object(server, "FRENCH", True)):
            p.start()
            self.addCleanup(p.stop)
        server._timers.clear()

    def test_timer_shows_rings_and_is_gone(self):
        r = call("start", {"minutes": 10, "label": "pâtes"})["structuredContent"]
        self.assertEqual((r["kind"], r["ends"], r["remaining_s"]), ("timer", "14:10", 600))
        self.assertEqual(self.events[-1], ("timers.activity", {"key": r["id"], "text": "pâtes", "sub": "fin à 14:10"}))
        self.clock[0] += 599
        self.assertEqual(server.due(), [])
        self.clock[0] += 1
        for t in server.due():
            server.ring(t)
        kinds = [k for k, _ in self.events]
        self.assertIn("timers.done", kinds)
        self.assertEqual(self.events[-2][1]["text"], "pâtes terminé")
        self.assertEqual(call("list", {})["structuredContent"]["timers"], [])

    def test_alarm_is_the_next_such_time(self):
        self.assertEqual(call("start", {"at": "7h30"})["structuredContent"]["remaining_s"], 17 * 3600 + 1800)
        self.assertEqual(call("start", {"at": "15:00"})["structuredContent"]["remaining_s"], 3600)
        for bad in ("25:00", "7:75", "midi"):
            self.assertTrue(call("start", {"at": bad})["isError"])
        for bad in ({}, {"minutes": -1}, {"minutes": "x"}, {"minutes": 99999}):
            self.assertTrue(call("start", bad)["isError"])

    def test_survives_a_restart_and_announces_late_ones(self):
        a = call("start", {"minutes": 5})["structuredContent"]["id"]
        b = call("start", {"minutes": 60, "label": "four"})["structuredContent"]["id"]
        server._timers.clear()
        self.events.clear()
        self.clock[0] += 600  # down for 10 minutes
        server.start_up()
        texts = [(d.get("key"), d.get("text")) for k, d in self.events if k == "timers.activity"]
        self.assertIn((a, "Minuteur terminé pendant l'absence"), texts)
        self.assertIn((b, "four"), texts)
        self.assertEqual([t["id"] for t in server._timers], [b])

    def test_cancel_by_label_or_all(self):
        call("start", {"minutes": 5, "label": "Thé"})
        call("start", {"minutes": 9})
        self.assertEqual(len(call("cancel", {"id": "thé"})["structuredContent"]["cancelled"]), 1)
        self.assertTrue(call("cancel", {"id": "nope"})["isError"])
        self.assertEqual(len(call("cancel", {"id": "all"})["structuredContent"]["cancelled"]), 1)
        self.assertEqual(self.events[-1][1].keys(), {"key"}, "a cancelled timer leaves the island")

    def test_protocol(self):
        tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["start", "list", "cancel"])
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(server.handle({"jsonrpc": "2.0", "id": 2, "method": "x"})["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
