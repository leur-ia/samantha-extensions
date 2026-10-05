"""python3 -m unittest discover extensions/calendar"""

import datetime
import unittest
from unittest import mock

import server

EVENTS = {"events": [
    {"title": "Anniversaire", "all_day": True, "start": "2026-10-08", "end": "2026-10-09 00:00"},
    {"title": "Stand-up", "all_day": False, "start": "2026-10-08 09:30", "end": "2026-10-08 09:45"},
    {"title": "Revue", "all_day": False, "start": "2026-10-08 10:00", "end": "2026-10-08 11:30"},
    {"title": "Chevauche", "all_day": False, "start": "2026-10-08 11:00", "end": "2026-10-08 12:00"},
    {"title": "Déjeuner", "all_day": False, "start": "2026-10-08 12:30", "end": "2026-10-08 18:30"},
    {"title": "Demain", "all_day": False, "start": "2026-10-09 08:00", "end": "2026-10-09 10:00"},
], "errors": ["caldav: ik: serveur injoignable (réseau ?)"]}


BUSY = {"busy": [
    {"email": "Fred@corp.com", "seen": True},
    {"email": "fred@corp.com", "start": "2026-10-08 09:00", "end": "2026-10-08 09:30"},
    {"email": "fred@corp.com", "start": "2026-10-08 15:00", "end": "2026-10-08 16:00"},
    {"email": "out@else.com", "seen": False, "reason": "google:me@corp.com: notFound"}],
    "errors": ["microsoft: me@outlook.com: Microsoft refuse l'accès (403)"]}


def call(name, args):
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args}})["result"]


class Free(unittest.TestCase):
    def setUp(self):
        self.asked = []

        def daemon(method, params, timeout=50):
            self.asked.append((method, params))
            if params["method"] == "busy":
                return BUSY
            return EVENTS

        for p in (mock.patch.object(server, "daemon", daemon), mock.patch.object(server, "FRENCH", True),
                  mock.patch.object(server, "now", lambda: datetime.datetime(2026, 10, 7, 20, 0))):
            p.start()
            self.addCleanup(p.stop)

    def slots(self, args):
        r = call("free", args)
        self.assertFalse(r["isError"], r)
        return r["structuredContent"]

    def test_gaps_between_merged_events(self):
        out = self.slots({"date": "2026-10-08", "days": 2})
        self.assertEqual(self.asked, [("capability.call", {"method": "events", "args": {"date": "2026-10-08", "days": 2}})])
        self.assertEqual([(s["day"], s["start"], s["end"]) for s in out["slots"]], [
            ("demain", "09:00", "09:30"), ("demain", "12:00", "12:30"),
            ("vendredi 9", "10:00", "18:00")])
        self.assertEqual(out["errors"], EVENTS["errors"])

    def test_minutes_hours_and_today_from_now(self):
        out = self.slots({"date": "2026-10-08", "minutes": 45, "from": "08:00", "to": "13:00"})
        self.assertEqual([(s["start"], s["end"], s["minutes"]) for s in out["slots"]], [("08:00", "09:30", 90)])
        with mock.patch.object(server, "now", lambda: datetime.datetime(2026, 10, 8, 11, 52, 30)):
            out = self.slots({})
        self.assertEqual(self.asked[-1][1]["args"]["date"], "2026-10-08")
        self.assertEqual([(s["day"], s["start"]) for s in out["slots"]], [("aujourd'hui", "12:00")])

    def test_with_other_people(self):
        out = self.slots({"date": "2026-10-08", "with": ["fred@corp.com", "out@else.com"], "from": "08:00"})
        self.assertEqual(self.asked[-1], ("capability.call", {"method": "busy", "args": {
            "emails": ["fred@corp.com", "out@else.com"], "date": "2026-10-08", "days": 1}}))
        self.assertEqual([(s["start"], s["end"]) for s in out["slots"]], [("08:00", "09:00"), ("12:00", "12:30")],
                         "Fred's 09:00 joins the stand-up; his 15:00 falls in lunch")
        self.assertEqual(out["unseen"], ["out@else.com"])
        self.assertEqual(len(out["errors"]), 2)
        self.assertTrue(call("free", {"with": ["Frédéric"]})["isError"])

    def test_with_and_no_provider_for_busy(self):
        def daemon(method, params, timeout=50):
            if params["method"] == "busy":
                raise server.Failure("Agenda: no provider implements calendar.busy")
            return {"events": []}
        with mock.patch.object(server, "daemon", daemon):
            out = self.slots({"date": "2026-10-08", "with": "fred@corp.com"})
        self.assertEqual((out["unseen"], out["slots"][0]["minutes"]), (["fred@corp.com"], 540))
        self.assertIn("calendar.busy", out["errors"][0])

    def test_bad_arguments_and_daemon_down(self):
        self.assertTrue(call("free", {"date": "jeudi"})["isError"])
        self.assertTrue(call("free", {"from": "18:00", "to": "09:00"})["isError"])
        with mock.patch.object(server, "daemon", mock.Mock(side_effect=server.Failure("Le démon de Samantha n'est pas joignable"))):
            self.assertIn("démon", call("free", {})["content"][0]["text"])

    def test_protocol(self):
        tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["free"])
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(server.handle({"jsonrpc": "2.0", "id": 2, "method": "nope"})["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
