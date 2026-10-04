"""python3 -m unittest discover contrib/calendar"""

import datetime
import io
import os
import tempfile
import unittest
from unittest import mock

import server

UTC = datetime.timezone.utc
PARIS = server.zoneinfo.ZoneInfo("Europe/Paris")

FEED = """BEGIN:VCALENDAR\r
BEGIN:VEVENT\r
UID:standup\r
SUMMARY:Stand-up\\, équipe\r
DTSTART;TZID=Europe/Paris:20260928T093000\r
DTEND;TZID=Europe/Paris:20260928T094500\r
RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR;UNTIL=20261231T000000Z\r
EXDATE;TZID=Europe/Paris:20261007T093000\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:standup\r
RECURRENCE-ID;TZID=Europe/Paris:20261005T093000\r
SUMMARY:Stand-up (décalé)\r
DTSTART;TZID=Europe/Paris:20261005T110000\r
DTEND;TZID=Europe/Paris:20261005T111500\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:birthday\r
SUMMARY:Anniversaire Léa\r
DTSTART;VALUE=DATE:20201006\r
RRULE:FREQ=YEARLY\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:dentist\r
SUMMARY:Dentiste\r
LOCATION:12 rue de\r
 France\r
DTSTART:20261006T130000Z\r
DURATION:PT30M\r
END:VEVENT\r
END:VCALENDAR\r
"""


def window(y, m, d, days=1):
    start = datetime.datetime(y, m, d, tzinfo=PARIS)
    return start, start + datetime.timedelta(days=days)


class Ics(unittest.TestCase):
    def test_parse_unfold_unescape(self):
        events = {e["uid"] + e.get("summary", ""): e for e in server.parse_ics(FEED)}
        self.assertEqual(events["dentistDentiste"]["location"], "12 rue deFrance")
        self.assertIn("standupStand-up, équipe", events)

    def test_weekly_with_exdate_and_moved_instance(self):
        found = server.expand(server.parse_ics(FEED), *window(2026, 10, 5, 5))
        standups = [(e["title"], e["start"].astimezone(PARIS).strftime("%a %H:%M"))
                    for e in sorted(found, key=lambda e: server.as_dt(e["start"])) if e["uid"] == "standup"]
        self.assertEqual(standups, [("Stand-up (décalé)", "Mon 11:00"), ("Stand-up, équipe", "Fri 09:30")])

    def test_yearly_all_day_and_duration(self):
        found = server.expand(server.parse_ics(FEED), *window(2026, 10, 6))
        titles = {e["title"]: e for e in found}
        self.assertTrue(titles["Anniversaire Léa"]["all_day"])
        d = titles["Dentiste"]
        self.assertEqual(d["end"] - d["start"], datetime.timedelta(minutes=30))

    def test_count_and_daily(self):
        e = {"start": datetime.datetime(2026, 10, 1, 8, tzinfo=UTC), "end": datetime.datetime(2026, 10, 1, 9, tzinfo=UTC),
             "rrule": "FREQ=DAILY;INTERVAL=2;COUNT=3", "exdates": []}
        got = server.occurrences(e, datetime.datetime(2026, 9, 1, tzinfo=UTC), datetime.datetime(2026, 11, 1, tzinfo=UTC))
        self.assertEqual([t.day for t in got], [1, 3, 5])


MULTI = """<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">{}</d:multistatus>"""
PRINCIPAL = MULTI.format("<d:response><d:href>/</d:href><d:propstat><d:prop><d:current-user-principal>"
                         "<d:href>/principals/me/</d:href></d:current-user-principal></d:prop></d:propstat></d:response>")
HOME = MULTI.format("<d:response><d:href>/principals/me/</d:href><d:propstat><d:prop><c:calendar-home-set>"
                    "<d:href>/calendars/me/</d:href></c:calendar-home-set></d:prop></d:propstat></d:response>")
CALS = MULTI.format(
    "<d:response><d:href>/calendars/me/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>"
    "<d:response><d:href>/calendars/me/perso/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
    "<d:displayname>Perso</d:displayname></d:prop></d:propstat></d:response>")
REPORT = MULTI.format(
    "<d:response><d:href>/calendars/me/perso/a.ics</d:href><d:propstat><d:prop><c:calendar-data>"
    "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:a\nSUMMARY:Réunion\nDTSTART:20261006T080000Z\nDTEND:20261006T090000Z\n"
    "END:VEVENT\nEND:VCALENDAR\n</c:calendar-data></d:prop></d:propstat></d:response>")


class Server:
    def __init__(self):
        self.requests = []

    def __call__(self, req, timeout=None):
        assert timeout and req.get_header("Authorization").startswith("Basic ")
        self.requests.append(req)
        path = req.full_url.removeprefix("https://dav.example")
        body = {("PROPFIND", "/.well-known/caldav"): PRINCIPAL, ("PROPFIND", "/principals/me/"): HOME,
                ("PROPFIND", "/calendars/me/"): CALS, ("REPORT", "/calendars/me/perso/"): REPORT}.get((req.get_method(), path), "")
        r = io.BytesIO(body.encode())
        r.geturl = lambda: req.full_url
        return r


def call(name, arguments):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    return r["result"]


class CalDav(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        os.makedirs(os.path.join(d.name, "samantha"))
        with open(os.path.join(d.name, "samantha", "calendar.toml"), "w") as f:
            f.write('[accounts.ik]\nurl = "https://dav.example/"\nuser = "me"\n')
        self.srv = Server()
        server._calendars.clear()
        for p in (mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d.name}),
                  mock.patch.object(server, "secret", lambda a: "pw"),
                  mock.patch("urllib.request.urlopen", self.srv)):
            p.start()
            self.addCleanup(p.stop)

    def test_discovery_and_expanded_query(self):
        r = call("events", {"date": "2026-10-06"})["structuredContent"]
        self.assertEqual([(e["title"], e["calendar"]) for e in r["events"]], [("Réunion", "Perso")])
        report = self.srv.requests[-1]
        self.assertIn(b"<c:expand", report.data)
        self.assertEqual(call("calendars", {})["structuredContent"]["calendars"],
                         [{"account": "ik", "calendar": "Perso", "writable": True}])

    def test_create_puts_an_event(self):
        r = call("create", {"title": "Dîner; chez Ana", "start": "2026-10-10 20:00", "minutes": 120})
        self.assertFalse(r["isError"], r)
        put = self.srv.requests[-1]
        self.assertEqual((put.get_method(), put.get_header("If-none-match")), ("PUT", "*"))
        self.assertTrue(put.full_url.startswith("https://dav.example/calendars/me/perso/"))
        self.assertIn(b"SUMMARY:D\xc3\xaener\\; chez Ana", put.data)
        self.assertTrue(call("create", {"title": "x", "start": "demain"})["isError"])

    def test_reminder_once_per_event(self):
        published = []
        soon = datetime.datetime(2026, 10, 6, 7, 52, tzinfo=UTC)
        with mock.patch.object(server, "now", lambda: soon), \
             mock.patch.object(server, "publish", lambda k, d: published.append((k, d))):
            server._announced.clear()
            server.remind_once()
            server.remind_once()
        kinds = [k for k, _ in published]
        self.assertEqual(kinds, ["calendar.activity", "calendar.soon"])
        self.assertIn("Réunion", published[0][1]["text"])

    def test_no_config(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/nonexistent"}):
            self.assertIn("calendar.toml", call("events", {})["content"][0]["text"])

    def test_protocol(self):
        tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["events", "calendars", "create"])


if __name__ == "__main__":
    unittest.main()
