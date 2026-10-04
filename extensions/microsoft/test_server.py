"""python3 -m unittest discover extensions/microsoft"""

import io
import json
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import server

INBOX = {"value": [
    {"id": "AAMk1", "subject": "Facture", "isRead": False, "receivedDateTime": "2026-10-04T07:30:00Z",
     "from": {"emailAddress": {"name": "EDF", "address": "Facture@EDF.fr"}}},
    {"id": "AAMk0", "subject": "Hello", "isRead": True, "receivedDateTime": "2026-10-03T07:30:00Z",
     "from": {"emailAddress": {"name": "", "address": "ana@x.org"}}}]}


class Fake:
    """Daemon (oauth) and Graph stand-ins; records Graph requests."""

    def __init__(self, accounts=("me@outlook.com",)):
        self.accounts, self.requests = list(accounts), []

    def daemon(self, method, params, timeout=30):
        assert params["provider"] == "microsoft"
        if method == "oauth.accounts":
            return {"accounts": self.accounts}
        return {"access_token": f"tok-{params['account']}", "account": params["account"]}

    def urlopen(self, req, timeout=None):
        assert timeout
        url = urllib.parse.urlparse(req.full_url)
        self.requests.append((req.get_method(), url.path, dict(urllib.parse.parse_qsl(url.query)),
                              json.loads(req.data) if req.data else None, req.get_header("Authorization")))
        path = url.path.removeprefix("/v1.0")
        if path.endswith("/messages") and req.get_method() == "GET":
            body = INBOX
        elif path == "/me/messages/AAMk1":
            body = {**INBOX["value"][0], "body": {"content": "Bonjour"}, "toRecipients": [{"emailAddress": {"address": "me@outlook.com"}}],
                    "hasAttachments": False}
        elif path == "/me/calendarView":
            body = {"value": [{"subject": "Réunion", "start": {"dateTime": "2026-10-06T10:00:00.0000000"},
                               "end": {"dateTime": "2026-10-06T11:00:00.0000000"}, "isAllDay": False,
                               "location": {"displayName": "Salle 2"}, "bodyPreview": "",
                               "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/abc"}, "onlineMeetingProvider": "teamsForBusiness"},
                              {"subject": "Annulé", "isCancelled": True, "start": {"dateTime": "2026-10-06T12:00:00"},
                               "end": {"dateTime": "2026-10-06T13:00:00"}}]}
        elif path == "/me/calendars":
            body = {"value": [{"id": "c1", "name": "Calendrier", "canEdit": True}]}
        else:
            body = {"id": "NEW"}
        return io.BytesIO(json.dumps(body).encode())


def call(name, args):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    return r["result"]


class Microsoft(unittest.TestCase):
    def setUp(self):
        self.f = Fake()
        for p in (mock.patch.object(server, "daemon", self.f.daemon), mock.patch("urllib.request.urlopen", self.f.urlopen),
                  mock.patch.object(server, "local_zone", lambda: "Europe/Paris")):
            p.start()
            self.addCleanup(p.stop)

    def use(self, tools):
        p = mock.patch.object(server, "TOOLS", tools)
        p.start()
        self.addCleanup(p.stop)

    def test_list_rows_and_unread_filter(self):
        self.use(server.MAIL)
        rows = call("list", {"unread_only": True, "limit": 5})["structuredContent"]["rows"]
        self.assertEqual(rows[0]["id"], "me@outlook.com/AAMk1")
        self.assertEqual((rows[0]["from"], rows[0]["name"], rows[0]["unread"]), ("facture@edf.fr", "EDF", True))
        self.assertTrue(rows[0]["date"] > rows[1]["date"])
        method, path, query, _, auth = self.f.requests[0]
        self.assertEqual((path, query["$filter"], query["$top"]), ("/v1.0/me/mailFolders/inbox/messages", "isRead eq false", "5"))
        self.assertEqual(auth, "Bearer tok-me@outlook.com")

    def test_several_accounts_and_search_syntax(self):
        self.f.accounts = ["me@outlook.com", "pro@corp.com"]
        self.use(server.MAIL)
        r = call("search", {"query": "from:edf since:2026-09-01 is:unread"})["structuredContent"]
        self.assertEqual({row["account"] for row in r["rows"]}, {"me@outlook.com", "pro@corp.com"})
        self.assertEqual(len(r["rows"]), 4)
        self.assertEqual(self.f.requests[0][2]["$search"], '"from:edf received>=2026-09-01 isread:false"')
        self.assertTrue(call("list", {"account": "nope@x"})["isError"])

    def test_read_mark_send_reply(self):
        self.use(server.MAIL)
        m = call("read", {"id": "me@outlook.com/AAMk1"})["structuredContent"]
        self.assertEqual((m["body"], m["to"]), ("Bonjour", "me@outlook.com"))
        call("mark_read", {"id": "me@outlook.com/AAMk1"})
        self.assertEqual(self.f.requests[-1][:2], ("PATCH", "/v1.0/me/messages/AAMk1"))
        call("send", {"to": "Ana <ana@x.org>", "subject": "Hi", "body": "Yo"})
        method, path, _, body, _ = self.f.requests[-1]
        self.assertEqual((method, path), ("POST", "/v1.0/me/sendMail"))
        self.assertEqual(body["message"]["toRecipients"], [{"emailAddress": {"address": "ana@x.org", "name": "Ana"}}])
        call("send", {"to": "x@y", "subject": "Re", "body": "Oui", "reply_to_id": "me@outlook.com/AAMk1"})
        self.assertEqual(self.f.requests[-1][1:4:2], ("/v1.0/me/messages/AAMk1/reply", {"comment": "Oui"}))
        self.assertTrue(call("send", {"to": "pas-une-adresse", "subject": "s", "body": "b"})["isError"])
        self.assertTrue(call("read", {"id": "sans-compte"})["isError"])

    def test_calendar(self):
        self.use(server.CALENDAR)
        ev = call("events", {"date": "2026-10-06"})["structuredContent"]["events"]
        self.assertEqual([(e["title"], e["start"], e["location"]) for e in ev], [("Réunion", "2026-10-06 10:00", "Salle 2")])
        self.assertEqual(self.f.requests[-1][2]["startDateTime"], "2026-10-06T00:00:00")
        self.assertEqual((ev[0]["meeting"], ev[0]["meeting_url"]), ("Teams", "https://teams.microsoft.com/l/meetup-join/abc"))
        self.assertEqual(server.meeting("Lien : https://meet.google.com/abc-defg-hij")["meeting"], "Google Meet")
        call("create", {"title": "Dîner", "start": "2026-10-10 20:00", "calendar": "calend"})
        method, path, _, body, _ = self.f.requests[-1]
        self.assertEqual((method, path, body["start"]["timeZone"]), ("POST", "/v1.0/me/calendars/c1/events", "Europe/Paris"))
        self.assertEqual(call("calendars", {})["structuredContent"]["calendars"][0]["writable"], True)

    def test_errors(self):
        self.use(server.MAIL)
        self.f.accounts = []
        self.assertIn("samantha account add microsoft", call("list", {})["content"][0]["text"])
        self.f.accounts = ["me@outlook.com"]
        denied = lambda req, timeout=None: (_ for _ in ()).throw(urllib.error.HTTPError(req.full_url, 403, "no", {}, io.BytesIO(b'{"error": {"message": "Access is denied."}}')))
        with mock.patch("urllib.request.urlopen", denied):
            r = call("read", {"id": "me@outlook.com/AAMk1"})
        self.assertIn("consentement", r["content"][0]["text"])

    def test_protocol(self):
        self.use(server.MAIL)
        names = [t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
        self.assertEqual(names, ["accounts", "list", "search", "read", "mark_read", "draft", "send"])
        self.assertEqual(list(server.CALENDAR), ["events", "calendars", "create"])


if __name__ == "__main__":
    unittest.main()
