"""python3 -m unittest discover extensions/google"""

import base64
import datetime
import io
import json
import unittest
import urllib.parse
from unittest import mock

import server

b64 = lambda s: base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")
MSG = {"id": "18c2", "threadId": "t1", "internalDate": "1791100000000", "labelIds": ["INBOX", "UNREAD"],
       "payload": {"mimeType": "multipart/alternative", "headers": [
           {"name": "From", "value": "EDF <Facture@EDF.fr>"}, {"name": "Subject", "value": "Facture"},
           {"name": "To", "value": "me@gmail.com"}, {"name": "Message-ID", "value": "<abc@edf.fr>"}],
           "parts": [{"mimeType": "text/html", "body": {"data": b64("<p>Bonjour&nbsp;!</p><script>x</script>")}},
                     {"mimeType": "application/pdf", "filename": "facture.pdf", "body": {"size": 1200}}]}}


class Fake:
    def __init__(self):
        self.requests = []
        self.scopes = [server.DIRECTORY]

    def daemon(self, method, params, timeout=30):
        assert params["provider"] == "google"
        if method == "oauth.accounts":
            return {"accounts": ["me@gmail.com"], "scopes": {"me@gmail.com": self.scopes}}
        return {"access_token": "tok"}

    def urlopen(self, req, timeout=None):
        assert timeout and req.get_header("Authorization") == "Bearer tok"
        u = urllib.parse.urlparse(req.full_url)
        q = urllib.parse.parse_qs(u.query)
        body = json.loads(req.data) if req.data else None
        self.requests.append((req.get_method(), u.path, q, body))
        p = u.path
        if p.endswith("/messages") and req.get_method() == "GET":
            out = {"messages": [{"id": "18c2"}]}
        elif "/messages/18c2" in p and req.get_method() == "GET":
            out = MSG
        elif p.endswith("/calendarList"):
            out = {"items": [{"id": "me@gmail.com", "summary": "Perso", "accessRole": "owner", "selected": True},
                             {"id": "fr.french#holiday@group.v.calendar.google.com", "summary": "Fériés", "accessRole": "reader"}]}
        elif p.endswith("/events/e1") and req.get_method() == "GET":
            out = {"id": "e1", "summary": "Stand-up", "start": {"dateTime": "2026-10-06T09:30:00+02:00"},
                   "end": {"dateTime": "2026-10-06T09:45:00+02:00"}}
        elif p.endswith("/events") and req.get_method() == "GET":
            out = {"items": [{"id": "e1", "summary": "Stand-up", "start": {"dateTime": "2026-10-06T09:30:00+02:00"},
                              "end": {"dateTime": "2026-10-06T09:45:00+02:00"}, "hangoutLink": "https://meet.google.com/abc-defg-hij"}]}
        elif p.endswith("/freeBusy"):
            out = {"calendars": {
                "fred@corp.com": {"busy": [{"start": "2026-10-06T08:00:00Z", "end": "2026-10-06T09:00:00Z"}]},
                "out@else.com": {"errors": [{"domain": "global", "reason": "notFound"}], "busy": []}}}
        elif "searchDirectoryPeople" in p:
            out = {"people": [{"resourceName": "people/d1", "names": [{"displayName": "Frédéric Roux"}],
                               "emailAddresses": [{"value": "fred@corp.com"}], "organizations": [{"name": "Corp"}]},
                              {"resourceName": "people/d2", "names": [{"displayName": "Ana Martin"}],
                               "emailAddresses": [{"value": "ana@x.org"}]}]}
        elif p.endswith("/events") and req.get_method() == "POST":
            out = {"id": "new", "hangoutLink": "https://meet.google.com/new-link-abc"} if body.get("conferenceData") else {"id": "new"}
        elif "searchContacts" in p:
            out = {"results": [{"person": {"resourceName": "people/c1", "names": [{"displayName": "Ana Martin"}],
                                           "emailAddresses": [{"value": "ana@x.org"}], "phoneNumbers": [{"value": "+33 6 12"}]}}]}
        elif "otherContacts" in p:
            out = {"results": [{"person": {"resourceName": "otherContacts/o1", "names": [{"displayName": "Ana M."}],
                                           "emailAddresses": [{"value": "ana@x.org"}]}},
                               {"person": {"resourceName": "otherContacts/o2", "emailAddresses": [{"value": "ana.pro@corp.com"}]}}]}
        else:
            out = {"id": "new", "message": {"id": "new"}}
        return io.BytesIO(json.dumps(out).encode())


def call(name, args):
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})["result"]


class Google(unittest.TestCase):
    def setUp(self):
        self.f = Fake()
        for p in (mock.patch.object(server, "daemon", self.f.daemon), mock.patch("urllib.request.urlopen", self.f.urlopen),
                  mock.patch.object(server, "local_zone", lambda: "Europe/Paris")):
            p.start()
            self.addCleanup(p.stop)

    def role(self, name):
        p = mock.patch.object(server, "TOOLS", server.ROLES[name])
        p.start()
        self.addCleanup(p.stop)

    def test_gmail_list_read(self):
        self.role("mail")
        rows = call("list", {"unread_only": True})["structuredContent"]["rows"]
        self.assertEqual((rows[0]["id"], rows[0]["from"], rows[0]["name"], rows[0]["unread"]),
                         ("me@gmail.com/18c2", "facture@edf.fr", "EDF", True))
        self.assertEqual(self.f.requests[0][2]["q"], ["is:unread in:inbox"])
        m = call("read", {"id": "me@gmail.com/18c2"})["structuredContent"]
        self.assertEqual((m["body"], m["attachments"]), ("Bonjour\xa0!", [{"name": "facture.pdf", "size": 1200}]))
        self.assertTrue(call("read", {"id": "me@gmail.com/../x"})["isError"])

    def test_search_syntax_and_reply(self):
        self.role("mail")
        call("search", {"query": "from:edf since:2026-09-01 is:unread"})
        self.assertEqual(self.f.requests[0][2]["q"], ["from:edf after:2026/09/01 is:unread"])
        call("send", {"to": "facture@edf.fr", "subject": "Re: Facture", "body": "Merci", "reply_to_id": "me@gmail.com/18c2"})
        method, path, _, body = self.f.requests[-1]
        self.assertEqual((method, path.endswith("/messages/send"), body["threadId"]), ("POST", True, "t1"))
        raw = base64.urlsafe_b64decode(body["raw"]).decode()
        self.assertIn("In-Reply-To: <abc@edf.fr>", raw)
        self.assertTrue(call("send", {"to": "pas-une-adresse", "subject": "s", "body": "b"})["isError"])

    def test_calendar_meet_and_create(self):
        self.role("calendar")
        ev = call("events", {"date": "2026-10-06"})["structuredContent"]["events"]
        self.assertEqual([(e["title"], e["start"], e["meeting"]) for e in ev], [("Stand-up", "2026-10-06 09:30", "Google Meet")] * 2)
        cals = call("calendars", {})["structuredContent"]["calendars"]
        self.assertEqual([c["writable"] for c in cals], [True, False])
        call("create", {"title": "Dîner", "start": "2026-10-10 20:00", "calendar": "perso"})
        method, path, _, body = self.f.requests[-1]
        self.assertEqual((method, path, body["start"]["timeZone"]), ("POST", "/calendar/v3/calendars/me%40gmail.com/events", "Europe/Paris"))

    def test_calendar_update_and_delete(self):
        self.role("calendar")
        ev = call("events", {"date": "2026-10-06"})["structuredContent"]["events"][0]
        self.assertEqual(ev["id"], "me@gmail.com/me@gmail.com/e1")
        r = call("update", {"id": ev["id"], "event": "stand-up", "start": "2026-10-06 10:00"})["structuredContent"]
        method, path, _, body = self.f.requests[-1]
        self.assertEqual((method, path), ("PATCH", "/calendar/v3/calendars/me%40gmail.com/events/e1"))
        self.assertEqual((body["start"]["dateTime"], body["end"]["dateTime"]), ("2026-10-06T10:00:00", "2026-10-06T10:15:00"),
                         "the length is kept")
        self.assertEqual((r["updated"], r["start"]), ("Stand-up", "2026-10-06 10:00"))
        call("update", {"id": ev["id"], "event": "Stand-up", "title": "Daily", "minutes": 30})
        body = self.f.requests[-1][3]
        self.assertEqual((body["summary"], body["start"]["dateTime"], body["end"]["dateTime"]),
                         ("Daily", "2026-10-06T09:30:00", "2026-10-06T10:00:00"))
        self.assertEqual(call("delete", {"id": ev["id"], "event": "Stand-up"})["structuredContent"]["deleted"], "Stand-up")
        self.assertEqual(self.f.requests[-1][:2], ("DELETE", "/calendar/v3/calendars/me%40gmail.com/events/e1"))
        n = len(self.f.requests)
        wrong = call("delete", {"id": ev["id"], "event": "Dentiste"})
        self.assertTrue(wrong["isError"])
        self.assertIn("Stand-up", wrong["content"][0]["text"])
        self.assertEqual([r[0] for r in self.f.requests[n:]], ["GET"], "nothing deleted")
        self.assertTrue(call("update", {"id": ev["id"], "event": "Stand-up"})["isError"], "nothing to change")
        self.assertTrue(call("delete", {"id": "me@gmail.com/../e1", "event": "Stand-up"})["isError"])

    def test_contacts_merge_saved_directory_and_other(self):
        self.role("contacts")
        rows = call("search", {"query": "ana"})["structuredContent"]["contacts"]
        self.assertEqual([(r["name"], r["emails"], r["kind"]) for r in rows],
                         [("Ana Martin", ["ana@x.org"], "contact"), ("Frédéric Roux", ["fred@corp.com"], "directory"),
                          ("", ["ana.pro@corp.com"], "other")])
        q = [r[2] for r in self.f.requests if "searchDirectoryPeople" in r[1]][0]
        self.assertEqual(q["sources"], ["DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE", "DIRECTORY_SOURCE_TYPE_DOMAIN_CONTACT"])
        self.f.scopes, self.f.requests = [], []
        rows = call("search", {"query": "ana"})["structuredContent"]["contacts"]
        self.assertNotIn("directory", [r["kind"] for r in rows], "not granted: not asked")
        self.assertFalse([r for r in self.f.requests if "searchDirectoryPeople" in r[1]])

    def test_invitations_meet_and_busy(self):
        self.role("calendar")
        r = call("create", {"title": "design", "start": "2026-10-06 14:30", "minutes": 30,
                            "attendees": ["fred@corp.com"], "meet": True})["structuredContent"]
        method, path, q, body = self.f.requests[-1]
        self.assertEqual((q["sendUpdates"], q["conferenceDataVersion"]), (["all"], ["1"]))
        self.assertEqual(body["attendees"], [{"email": "fred@corp.com"}])
        self.assertEqual(body["conferenceData"]["createRequest"]["conferenceSolutionKey"], {"type": "hangoutsMeet"})
        self.assertEqual((r["attendees"], r["meeting_url"]), (["fred@corp.com"], "https://meet.google.com/new-link-abc"))
        call("create", {"title": "seul", "start": "2026-10-06 16:00"})
        self.assertEqual(self.f.requests[-1][2]["sendUpdates"], ["none"])
        self.assertNotIn("attendees", self.f.requests[-1][3])
        self.assertTrue(call("create", {"title": "x", "start": "2026-10-06 16:00", "attendees": ["Frédéric"]})["isError"])

        rows = call("busy", {"emails": ["fred@corp.com", "out@else.com"], "date": "2026-10-06"})["structuredContent"]["busy"]
        self.assertEqual(rows[0], {"email": "fred@corp.com", "seen": True})
        local = lambda h: datetime.datetime(2026, 10, 6, h, tzinfo=datetime.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
        self.assertEqual(rows[1], {"email": "fred@corp.com", "start": local(8), "end": local(9)})
        self.assertEqual(rows[2], {"email": "out@else.com", "seen": False, "reason": "me@gmail.com: notFound"})
        sent = self.f.requests[-1][3]
        self.assertEqual((sent["items"], sent["timeZone"]), ([{"id": "fred@corp.com"}, {"id": "out@else.com"}], "Europe/Paris"))
        self.assertTrue(call("busy", {"emails": []})["isError"])

    def test_no_account(self):
        self.role("mail")
        with mock.patch.object(server, "daemon", lambda m, p, timeout=30: {"accounts": []}):
            self.assertIn("samantha account add google", call("list", {})["content"][0]["text"])

    def test_protocol(self):
        self.assertEqual(list(server.ROLES["mail"]), ["accounts", "list", "search", "read", "mark_read", "draft", "send"])
        self.assertEqual(list(server.ROLES["calendar"]), ["events", "calendars", "create", "busy", "update", "delete"])
        self.assertEqual(list(server.ROLES["contacts"]), ["search"])


if __name__ == "__main__":
    unittest.main()
