"""cd extensions/reddit && python3 -m unittest -q (no network: urlopen and the daemon are stand-ins)."""

import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import server

CONFIG = """
[reddit.perso]
username = "ana"
client_id = "cid"

[mastodon.perso]
instance = "mastodon.social"
"""
TOKEN = {"access_token": "tok-1", "expires_in": 3600}


class Response(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(b"" if body is None else json.dumps(body).encode())
        self.headers = headers or {}


class Seq(list):
    """Answers in turn, the last one repeated."""


class Reddit:
    """Stands in for urlopen: answers (method, path) from a table, records requests."""

    def __init__(self, answers):
        self.answers, self.requests = {("POST", "/api/v1/access_token"): TOKEN, **answers}, []

    def __call__(self, req, timeout=None):
        assert timeout
        url = urllib.parse.urlsplit(req.full_url)
        self.requests.append(req)
        answer = self.answers[(req.get_method(), url.path)]
        if isinstance(answer, Seq):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, Exception):
            raise answer
        return Response(answer, {"X-Ratelimit-Remaining": "99", "X-Ratelimit-Reset": "60"})

    def api_calls(self):
        return [r for r in self.requests if "access_token" not in r.full_url]

    def form(self, i=-1):
        return dict(urllib.parse.parse_qsl(self.api_calls()[i].data.decode()))

    def query(self, i=-1):
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.api_calls()[i].full_url).query))


def http_error(code, headers=None):
    return urllib.error.HTTPError("u", code, "no", headers or {}, io.BytesIO(b"{}"))


def link(name, title, created, **extra):
    return {"kind": "t3", "data": {"name": name, "title": title, "author": "bob",
                                   "created_utc": created, "subreddit": "python",
                                   "permalink": f"/r/python/comments/{name[3:]}/x/",
                                   "num_comments": 4, "score": 12, **extra}}


def comment(name, body, parent, replies=""):
    return {"kind": "t1", "data": {"name": name, "body": body, "author": "eve",
                                   "created_utc": 1_700_000_100, "subreddit": "python",
                                   "parent_id": parent, "link_id": "t3_abc", "score": 3,
                                   "permalink": f"/r/python/comments/abc/x/{name[3:]}/",
                                   "replies": replies}}


def children(*things):
    return {"kind": "Listing", "data": {"children": list(things)}}


def call(name, arguments):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    return r["result"]


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        path = os.path.join(self.dir, "social.toml")
        with open(path, "w") as f:
            f.write(CONFIG)
        for p in (mock.patch.dict(os.environ, {"SOCIAL_CONFIG": path}),
                  mock.patch.object(server, "daemon", self.daemon),
                  mock.patch.object(server, "TOKENS", {}),
                  mock.patch.object(server, "RATE", {"remaining": None, "reset": 0.0}),
                  mock.patch("time.sleep")):
            p.start()
            self.addCleanup(p.stop)
        self.asked = []

    def daemon(self, method, params, timeout=30):
        self.asked.append((method, params))
        return {"value": "s3cret:pass:word"}

    def run_with(self, answers, name, args):
        r = Reddit(answers)
        with mock.patch("urllib.request.urlopen", r):
            return call(name, args), r


class Setup(Base):
    def test_missing_config_says_how(self):
        with mock.patch.dict(os.environ, {"SOCIAL_CONFIG": os.path.join(self.dir, "none.toml")}):
            r = call("feed", {})
        self.assertTrue(r["isError"])
        self.assertIn("reddit.com/prefs/apps", r["content"][0]["text"])
        self.assertIn("otto provider set-key social-<nom>", r["content"][0]["text"])

    def test_other_providers_only_is_not_set_up(self):
        path = os.path.join(self.dir, "other.toml")
        with open(path, "w") as f:
            f.write('[mastodon.perso]\ninstance = "x"\n')
        with mock.patch.dict(os.environ, {"SOCIAL_CONFIG": path}):
            r = call("accounts", {})
        self.assertIn("n'est pas configuré", r["content"][0]["text"])

    def test_accounts(self):
        r = call("accounts", {})
        self.assertEqual(r["structuredContent"]["accounts"], [
            {"id": "perso", "account": "perso", "network": "reddit", "handle": "u/ana",
             "url": "https://reddit.com/user/ana"}])

    def test_token_fetched_once_and_reused(self):
        answers = {("GET", "/best"): children()}
        r = Reddit(answers)
        with mock.patch("urllib.request.urlopen", r):
            call("feed", {})
            call("feed", {})
        tok = [q for q in r.requests if "access_token" in q.full_url]
        self.assertEqual(len(tok), 1)
        self.assertEqual(self.asked, [("secrets.get", {"name": "social-perso"})])
        form = dict(urllib.parse.parse_qsl(tok[0].data.decode()))
        # The secret splits on the first colon: the password keeps its own.
        self.assertEqual(form, {"grant_type": "password", "username": "ana", "password": "pass:word"})
        self.assertEqual(tok[0].get_header("Authorization"), "Basic Y2lkOnMzY3JldA==")  # cid:s3cret
        api = r.api_calls()[0]
        self.assertEqual(api.get_header("Authorization"), "bearer tok-1")
        self.assertEqual(api.get_header("User-agent"), "linux:otto-reddit:0.2.0 (by /u/ana)")
        self.assertTrue(api.full_url.startswith("https://oauth.reddit.com/best"))

    def test_web_app_uses_the_refresh_token(self):
        with open(os.environ["SOCIAL_CONFIG"], "a") as f:
            f.write('\n[reddit.web]\nusername = "bo"\nclient_id = "wid"\nredirect_uri = "http://localhost:8080"\n')
        r = Reddit({("GET", "/best"): children()})
        with mock.patch("urllib.request.urlopen", r):
            call("feed", {"account": "web"})
        tok = [q for q in r.requests if "access_token" in q.full_url]
        form = dict(urllib.parse.parse_qsl(tok[0].data.decode()))
        self.assertEqual(form, {"grant_type": "refresh_token", "refresh_token": "pass:word"})

    def test_rejected_password_and_401(self):
        r, _ = self.run_with({("POST", "/api/v1/access_token"): {"error": "invalid_grant"}}, "feed", {})
        self.assertIn("refuse ces identifiants", r["content"][0]["text"])
        self.assertNotIn("pass", r["content"][0]["text"].replace("passe", ""))
        r, _ = self.run_with({("GET", "/best"): http_error(401)}, "feed", {})
        self.assertTrue(r["isError"])
        self.assertIn("set-key social-", r["content"][0]["text"])
        self.assertEqual(server.TOKENS, {})  # the stale token is dropped

    def test_retry_once_after_429(self):
        r, d = self.run_with({("GET", "/best"): Seq([http_error(429, {"Retry-After": "0"}),
                                                      children(link("t3_a", "A", 1))])}, "feed", {})
        self.assertFalse(r["isError"])
        self.assertEqual(len(d.api_calls()), 2)
        r, d = self.run_with({("GET", "/best"): http_error(429)}, "feed", {})
        self.assertIn("limite", r["content"][0]["text"])
        self.assertEqual(len(d.api_calls()), 2)


class Reads(Base):
    def test_feed_mapping(self):
        posts = children(link("t3_old", "Ancien", 1_600_000_000),
                         link("t3_new", "Nouveau", 1_700_000_000, selftext="Corps " * 200,
                              post_hint="image", url_overridden_by_dest="https://i.redd.it/x.png"))
        r, d = self.run_with({("GET", "/best"): posts}, "feed",
                             {"limit": 500, "since": "2022-01-01"})
        rows = r["structuredContent"]["posts"]
        self.assertEqual(len(rows), 1)
        p = rows[0]
        self.assertEqual({k: p[k] for k in ("id", "account", "network", "author", "handle", "time",
                                            "url", "replies", "likes", "community")},
                         {"id": "perso/t3_new", "account": "perso", "network": "reddit",
                          "author": "bob", "handle": "u/bob", "time": "2023-11-14T22:13:20Z",
                          "url": "https://reddit.com/r/python/comments/new/x/", "replies": 4,
                          "likes": 12, "community": "r/python"})
        self.assertTrue(p["text"].startswith("Nouveau\n\nCorps"))
        self.assertLess(len(p["text"]), 520)
        self.assertEqual(p["media"][0]["url"], "https://i.redd.it/x.png")
        self.assertEqual(d.query()["limit"], "100")

    def test_inbox_kinds_and_unread(self):
        inbox = children(
            {"kind": "t1", "data": {"name": "t1_r", "type": "comment_reply", "author": "eve",
                                    "body": "merci", "created_utc": 3, "new": True}},
            {"kind": "t1", "data": {"name": "t1_m", "type": "username_mention", "author": "zoe",
                                    "body": "u/ana regarde", "created_utc": 2, "new": False}},
            {"kind": "t4", "data": {"name": "t4_p", "author": "max", "subject": "Salut",
                                    "body": "un message", "created_utc": 1, "new": True}})
        r, d = self.run_with({("GET", "/message/inbox"): inbox}, "notifications", {})
        items = r["structuredContent"]["items"]
        self.assertEqual([(i["kind"], i["from"]) for i in items],
                         [("reply", "eve"), ("mention", "zoe"), ("message", "max")])
        self.assertEqual(items[0]["post_id"], "perso/t1_r")
        self.assertNotIn("post_id", items[2])
        self.assertEqual(items[2]["text"], "Salut: un message")
        self.assertEqual(d.query()["mark"], "false")
        r, _ = self.run_with({("GET", "/message/inbox"): inbox}, "notifications", {"unread": True})
        self.assertEqual([i["id"] for i in r["structuredContent"]["items"]], ["perso/t1_r", "perso/t4_p"])

    def test_notification_events_seeded_then_new_only(self):
        old = children({"kind": "t1", "data": {"name": "t1_o", "type": "comment_reply", "author": "eve",
                                               "body": "vieux", "created_utc": 1, "context": "/r/p/comments/a/x/o/?context=3"}})
        new = children({"kind": "t4", "data": {"name": "t4_p", "id": "p", "author": "max", "subject": "Salut",
                                               "body": "un message", "created_utc": 2}},
                       *old["data"]["children"])
        events = []
        with mock.patch.dict(os.environ, {"REDDIT_STATE": os.path.join(self.dir, "r", "n.json")}), \
                mock.patch("urllib.request.urlopen", Reddit({("GET", "/message/inbox"): Seq([old, new, new])})):
            for _ in range(3):
                server.check_notifications(events.append)
            self.assertEqual(server.load_state(), {"perso": ["perso/t4_p", "perso/t1_o"]})
        self.assertEqual(events, [{"account": "perso", "network": "reddit", "kind": "message", "from": "max",
                                   "handle": "u/max", "text": "Salut: un message",
                                   "url": "https://reddit.com/message/messages/p", "id": "perso/t4_p",
                                   "date": "1970-01-01T00:00:02Z"}])

    def test_search_routing(self):
        found = children(link("t3_s", "Trouvé", 5))
        r, d = self.run_with({("GET", "/search"): found}, "search", {"query": "rust async"})
        self.assertEqual(d.query(), {"q": "rust async", "sort": "new", "limit": "20", "raw_json": "1"})
        self.assertEqual(r["structuredContent"]["posts"][0]["id"], "perso/t3_s")
        r, d = self.run_with({("GET", "/r/python/new"): found}, "search", {"query": "r/python"})
        self.assertFalse(r["isError"])
        r, d = self.run_with({("GET", "/r/python/search"): found}, "search",
                             {"query": "r/python asyncio  tips"})
        self.assertEqual((d.query()["q"], d.query()["restrict_sr"]), ("asyncio  tips", "1"))
        r, d = self.run_with({("GET", "/search"): found}, "search", {"query": "r/pythonic? no"})
        self.assertEqual(d.query()["q"], "r/pythonic? no")

    def test_thread_of_post_and_comment(self):
        nested = comment("t1_c2", "sous-réponse", "t1_c1")
        tree = [children(link("t3_abc", "Fil", 1)),
                children(comment("t1_c1", "réponse", "t3_abc", children(nested)),
                         comment("t1_c3", "autre", "t3_abc"))]
        r, d = self.run_with({("GET", "/comments/abc"): tree}, "thread", {"id": "reddit:perso/t3_abc"})
        t = r["structuredContent"]
        self.assertEqual(t["post"]["id"], "perso/t3_abc")
        self.assertEqual(t["ancestors"], [])
        self.assertEqual([(x["id"], x["reply_to"]) for x in t["replies"]],
                         [("perso/t1_c1", "perso/t3_abc"), ("perso/t1_c2", "perso/t1_c1"),
                          ("perso/t1_c3", "perso/t3_abc")])
        info = children(comment("t1_c2", "sous-réponse", "t1_c1"))
        r, d = self.run_with({("GET", "/api/info"): info, ("GET", "/comments/abc"): tree},
                             "thread", {"id": "perso/t1_c2"})
        t = r["structuredContent"]
        self.assertEqual(t["post"]["id"], "perso/t1_c2")
        self.assertEqual([a["id"] for a in t["ancestors"]], ["perso/t3_abc", "perso/t1_c1"])
        self.assertEqual(d.query()["comment"], "c2")
        r, _ = self.run_with({}, "thread", {"id": "perso/xyz"})
        self.assertIn("invalide", r["content"][0]["text"])


class Writes(Base):
    def test_new_post_needs_title_and_community(self):
        r, d = self.run_with({}, "post", {"text": "Bonjour", "title": "Salut"})
        self.assertIn("subreddit", r["content"][0]["text"])
        r, d = self.run_with({}, "post", {"text": "Bonjour", "community": "r/france"})
        self.assertIn("titre", r["content"][0]["text"])
        self.assertEqual(d.requests, [])
        made = {"json": {"errors": [], "data": {"name": "t3_new", "url": "https://www.reddit.com/r/france/comments/new/salut/"}}}
        r, d = self.run_with({("POST", "/api/submit"): made}, "post",
                             {"text": "Bonjour", "title": "Salut", "community": "r/france"})
        self.assertEqual(r["structuredContent"]["id"], "perso/t3_new")
        self.assertEqual(d.form(), {"kind": "self", "sr": "france", "title": "Salut",
                                    "text": "Bonjour", "api_type": "json"})

    def test_reply_and_refusal(self):
        made = {"json": {"errors": [], "data": {"things": [{"kind": "t1", "data": {
            "name": "t1_me", "permalink": "/r/python/comments/abc/x/me/"}}]}}}
        r, d = self.run_with({("POST", "/api/comment"): made}, "post",
                             {"text": "D'accord", "reply_to": "reddit:perso/t1_c1"})
        self.assertEqual(r["structuredContent"], {"id": "perso/t1_me",
                                                  "url": "https://reddit.com/r/python/comments/abc/x/me/"})
        self.assertEqual(d.form()["thing_id"], "t1_c1")
        refused = {"json": {"errors": [["RATELIMIT", "trop vite", "ratelimit"]]}}
        r, _ = self.run_with({("POST", "/api/comment"): refused}, "post",
                             {"text": "x", "reply_to": "perso/t3_abc"})
        self.assertIn("RATELIMIT", r["content"][0]["text"])

    def test_vote_and_undo(self):
        r, d = self.run_with({("POST", "/api/vote"): {}}, "like", {"id": "reddit:perso/t3_abc"})
        self.assertEqual(r["structuredContent"], {"id": "perso/t3_abc", "liked": True})
        self.assertEqual((d.form()["id"], d.form()["dir"]), ("t3_abc", "1"))
        r, d = self.run_with({("POST", "/api/vote"): {}}, "like", {"id": "perso/t1_c1", "undo": True})
        self.assertEqual(r["structuredContent"]["liked"], False)
        self.assertEqual(d.form()["dir"], "0")
        r, _ = self.run_with({}, "like", {"id": "ailleurs/t3_abc"})
        self.assertIn("Pas de compte", r["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
