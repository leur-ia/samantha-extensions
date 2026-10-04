"""python3 -m unittest discover contrib/slack"""

import io
import json
import os
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import server


class Slack:
    """Stands in for urlopen: answers Web API methods from a table, records calls."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, req, timeout=None):
        assert timeout
        method = req.full_url.rsplit("/", 1)[1]
        params = dict(urllib.parse.parse_qsl(req.data.decode()))
        self.calls.append((method, params, req.get_header("Authorization")))
        answer = self.answers[method]
        if isinstance(answer, Exception):
            raise answer
        body = answer(params) if callable(answer) else answer
        return io.BytesIO(json.dumps(body).encode())


USERS = lambda p: {"ok": True, "user": {"name": "x", "profile": {"display_name": {"U1": "ana", "U2": "bob"}[p["user"]]}}}
CHANNELS = {"ok": True, "channels": [
    {"id": "C0123456", "name": "general", "is_member": True},
    {"id": "G0123456", "name": "secret", "is_private": True, "is_member": True},
    {"id": "D0123456", "is_im": True, "user": "U2"}], "response_metadata": {"next_cursor": ""}}


def call(name, arguments):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    return r["result"]


class Tools(unittest.TestCase):
    def setUp(self):
        server._users.clear()
        server._channels.clear()
        p = mock.patch.dict(os.environ, {"SLACK_TOKEN": "xoxp-secret-token"})
        p.start()
        self.addCleanup(p.stop)

    def run_with(self, answers, name, args):
        slack = Slack(answers)
        with mock.patch("urllib.request.urlopen", slack):
            return call(name, args), slack

    def test_channels_name_dms(self):
        r, _ = self.run_with({"conversations.list": CHANNELS, "users.info": USERS}, "channels", {})
        self.assertEqual(r["structuredContent"]["channels"], [
            {"id": "C0123456", "name": "general", "kind": "public", "member": True},
            {"id": "G0123456", "name": "secret", "kind": "private", "member": True},
            {"id": "D0123456", "name": "bob", "kind": "dm", "member": True}])

    def test_history_by_name_oldest_first(self):
        hist = {"ok": True, "messages": [
            {"ts": "1790000002.000200", "user": "U2", "text": "deux", "reply_count": 3},
            {"ts": "1790000001.000100", "user": "U1", "text": "un"}]}
        r, slack = self.run_with({"conversations.list": CHANNELS, "users.info": USERS,
                                  "conversations.history": hist}, "history", {"channel": "#General"})
        msgs = r["structuredContent"]["messages"]
        self.assertEqual([(m["user"], m["text"], m["replies"]) for m in msgs],
                         [("ana", "un", 0), ("bob", "deux", 3)])
        method, params, auth = slack.calls[1]
        self.assertEqual((method, params["channel"]), ("conversations.history", "C0123456"))
        self.assertEqual(auth, "Bearer xoxp-secret-token")

    def test_errors_explained(self):
        r, _ = self.run_with({"search.messages": {"ok": False, "error": "missing_scope",
                                                  "needed": "search:read"}}, "search", {"query": "x"})
        self.assertTrue(r["isError"])
        self.assertIn("search:read", r["content"][0]["text"])
        r, _ = self.run_with({}, "history", {"channel": ""})
        self.assertTrue(r["isError"])

    def test_retry_once_after_429(self):
        seen = []

        def flaky(p):
            seen.append(1)
            return {"ok": True, "channel": "C0123456", "ts": "1790000003.000300"}
        limited = urllib.error.HTTPError("u", 429, "slow", {"Retry-After": "0"}, io.BytesIO(b""))
        answers = {"chat.postMessage": flaky}
        slack = Slack(answers)
        first = [True]

        def opener(req, timeout=None):
            if first[0]:
                first[0] = False
                raise limited
            return slack(req, timeout)
        with mock.patch("urllib.request.urlopen", opener):
            r = call("send", {"channel": "C0123456", "text": "salut"})
        self.assertFalse(r["isError"])
        self.assertEqual(r["structuredContent"]["ts"], "1790000003.000300")

    def test_input_checked_before_the_api(self):
        r, slack = self.run_with({}, "react", {"channel": "C0123456", "ts": "nope", "emoji": "x"})
        self.assertTrue(r["isError"])
        r, slack = self.run_with({}, "send", {"channel": "C0123456", "text": "  "})
        self.assertTrue(r["isError"])
        self.assertEqual(slack.calls, [])

    def test_unread_needs_a_user_token(self):
        info = lambda p: {"ok": True, "channel": {"id": p["channel"]}}
        r, _ = self.run_with({"conversations.list": CHANNELS, "conversations.info": info}, "unread", {})
        self.assertTrue(r["isError"])
        info = lambda p: {"ok": True, "channel": {"unread_count_display": 2 if p["channel"] == "C0123456" else 0}}
        r, _ = self.run_with({"conversations.list": CHANNELS, "conversations.info": info,
                              "users.info": USERS}, "unread", {})
        self.assertEqual(r["structuredContent"]["unread"], [{"id": "C0123456", "name": "general", "unread": 2}])


class Tokens(unittest.TestCase):
    def test_missing_and_session_tokens(self):
        for env, needle in (({}, "set-key slack"), ({"SLACK_TOKEN": "xoxc-abc"}, "navigateur")):
            with mock.patch.dict(os.environ, env, clear=True):
                r = call("channels", {})
            self.assertTrue(r["isError"])
            self.assertIn(needle, r["content"][0]["text"])
            self.assertNotIn("xoxc-abc", r["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
