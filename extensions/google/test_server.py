"""python3 -m unittest discover extensions/google"""

import base64
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

    def daemon(self, method, params, timeout=30):
        assert params["provider"] == "google"
        return {"accounts": ["me@gmail.com"]} if method == "oauth.accounts" else {"access_token": "tok"}

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
        elif p.endswith("/events") and req.get_method() == "GET":
            out = {"items": [{"id": "e1", "summary": "Stand-up", "start": {"dateTime": "2026-10-06T09:30:00+02:00"},
                              "end": {"dateTime": "2026-10-06T09:45:00+02:00"}, "hangoutLink": "https://meet.google.com/abc-defg-hij"}]}
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

    def test_contacts_merge_saved_and_other(self):
        self.role("contacts")
        rows = call("search", {"query": "ana"})["structuredContent"]["contacts"]
        self.assertEqual([(r["name"], r["emails"], r["kind"]) for r in rows],
                         [("Ana Martin", ["ana@x.org"], "contact"), ("", ["ana.pro@corp.com"], "other")])

    def test_no_account(self):
        self.role("mail")
        with mock.patch.object(server, "daemon", lambda m, p, timeout=30: {"accounts": []}):
            self.assertIn("samantha account add google", call("list", {})["content"][0]["text"])

    def test_protocol(self):
        self.assertEqual(list(server.ROLES["mail"]), ["accounts", "list", "search", "read", "mark_read", "draft", "send"])
        self.assertEqual(list(server.ROLES["calendar"]), ["events", "calendars", "create"])
        self.assertEqual(list(server.ROLES["contacts"]), ["search"])


if __name__ == "__main__":
    unittest.main()
