"""python3 -m unittest discover contrib/discord"""

import io
import json
import os
import unittest
import urllib.error
from unittest import mock

import server

G1, G2 = "111111111111111111", "222222222222222222"
C1, C2, CAT = "333333333333333333", "444444444444444444", "555555555555555555"
M1 = "666666666666666666"


class Discord:
    """Stands in for urlopen: answers (method, path) from a table, records requests."""

    def __init__(self, answers):
        self.answers, self.requests = answers, []

    def __call__(self, req, timeout=None):
        assert timeout
        path = req.full_url.removeprefix(server.API)
        self.requests.append(req)
        answer = self.answers[(req.get_method(), path.split("?")[0])]
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(b"" if answer is None else json.dumps(answer).encode())


GUILDS = [{"id": G1, "name": "Amis"}, {"id": G2, "name": "Travail"}]
CHANNELS = [{"id": CAT, "type": 4, "name": "Général"},
            {"id": C2, "type": 2, "name": "vocal"},
            {"id": C1, "type": 0, "name": "discussion", "parent_id": CAT, "position": 1}]


def call(name, arguments):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    return r["result"]


class Tools(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {"DISCORD_TOKEN": "bot-secret"})
        p.start()
        self.addCleanup(p.stop)

    def run_with(self, answers, name, args):
        d = Discord(answers)
        with mock.patch("urllib.request.urlopen", d):
            return call(name, args), d

    def test_channels_by_server_name(self):
        r, d = self.run_with({("GET", "/users/@me/guilds"): GUILDS,
                              ("GET", f"/guilds/{G1}/channels"): CHANNELS}, "channels", {"server": "amis"})
        self.assertEqual(r["structuredContent"]["channels"],
                         [{"id": C1, "name": "discussion", "kind": "text", "category": "Général"}])
        self.assertEqual(d.requests[0].get_header("Authorization"), "Bot bot-secret")
        self.assertTrue(d.requests[0].get_header("User-agent").startswith("DiscordBot ("))

    def test_messages_oldest_first(self):
        msgs = [{"id": "2" * 18, "timestamp": "2026-10-04T10:05:00+00:00", "content": "deux",
                 "author": {"username": "bob"}, "member": {"nick": "Bobby"}},
                {"id": "1" * 18, "timestamp": "2026-10-04T10:00:00+00:00", "content": "un",
                 "author": {"username": "ana", "global_name": "Ana"},
                 "attachments": [{"filename": "photo.jpg"}]}]
        r, d = self.run_with({("GET", f"/channels/{C1}/messages"): msgs}, "messages",
                             {"channel": C1, "limit": 500})
        rows = r["structuredContent"]["messages"]
        self.assertEqual([(m["author"], m["text"]) for m in rows], [("Ana", "un"), ("Bobby", "deux")])
        self.assertEqual(rows[0]["attachments"], ["photo.jpg"])
        self.assertIn("limit=50", d.requests[0].full_url)

    def test_send_never_pings_and_checks_length(self):
        r, d = self.run_with({("POST", f"/channels/{C1}/messages"): {"id": M1}}, "send",
                             {"channel": C1, "text": "salut @everyone", "reply_to": M1})
        body = json.loads(d.requests[0].data)
        self.assertEqual(body["allowed_mentions"], {"parse": []})
        self.assertEqual(body["message_reference"], {"message_id": M1})
        r, d = self.run_with({}, "send", {"channel": C1, "text": "x" * 2001})
        self.assertTrue(r["isError"])
        self.assertEqual(d.requests, [])

    def test_react_encodes_emoji(self):
        path = f"/channels/{C1}/messages/{M1}/reactions/%F0%9F%91%8D/@me"
        r, d = self.run_with({("PUT", path): None}, "react", {"channel": C1, "message_id": M1, "emoji": "👍"})
        self.assertFalse(r["isError"])

    def test_ambiguous_and_forbidden(self):
        twins = GUILDS + [{"id": "999999999999999999", "name": "AMIS"}]
        r, _ = self.run_with({("GET", "/users/@me/guilds"): twins}, "channels", {"server": "amis"})
        self.assertIn("Plusieurs", r["content"][0]["text"])
        denied = urllib.error.HTTPError("u", 403, "no", {}, io.BytesIO(b"{}"))
        r, _ = self.run_with({("GET", f"/channels/{C1}/messages"): denied}, "messages", {"channel": C1})
        self.assertIn("permission", r["content"][0]["text"])
        r, _ = self.run_with({}, "messages", {"channel": "discussion"})
        self.assertIn("serveur", r["content"][0]["text"])

    def test_retry_once_after_429(self):
        calls = []

        def opener(req, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.HTTPError("u", 429, "slow", {}, io.BytesIO(b'{"retry_after": 0}'))
            return io.BytesIO(json.dumps(GUILDS).encode())
        with mock.patch("urllib.request.urlopen", opener):
            r = call("servers", {})
        self.assertEqual(len(r["structuredContent"]["servers"]), 2)


class Tokens(unittest.TestCase):
    def test_missing_and_rejected(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            r = call("servers", {})
        self.assertIn("set-key discord", r["content"][0]["text"])
        bad = urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b"{}"))
        with mock.patch.dict(os.environ, {"DISCORD_TOKEN": "t0ken"}), \
             mock.patch("urllib.request.urlopen", Discord({("GET", "/users/@me/guilds"): bad})):
            r = call("servers", {})
        self.assertTrue(r["isError"])
        self.assertNotIn("t0ken", r["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
