"""cd extensions/mastodon && python3 -m unittest -q (no network)"""

import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

import server

ONE = """
[mastodon.perso]
instance = "https://mastodon.social/"

[bluesky.perso]
handle = "moi.bsky.social"
"""
TWO = ONE + """
[mastodon.boulot]
instance = "https://fosstodon.org"
handle = "@jb@fosstodon.org"
"""


class Instances:
    """Stands in for urlopen: answers (method, url without query) from a table,
    records requests."""

    def __init__(self, answers):
        self.answers, self.requests = answers, []

    def __call__(self, req, timeout=None):
        assert timeout
        self.requests.append(req)
        answer = self.answers[(req.get_method(), req.full_url.split("?")[0])]
        if isinstance(answer, Then):
            answer = answer.next()
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(b"" if answer is None else json.dumps(answer).encode())


class Then:
    """Successive answers to the same request."""

    def __init__(self, *answers):
        self.answers = list(answers)

    def next(self):
        return self.answers.pop(0)


def http_error(code, body=b"{}", headers=None):
    return urllib.error.HTTPError("u", code, "no", headers or {}, io.BytesIO(body))


M = "https://mastodon.social"
ANA = {"username": "ana", "acct": "ana", "display_name": "Ana"}
BOB = {"username": "bob", "acct": "bob@piaille.fr", "display_name": ""}
ORIGINAL = {"id": "111", "created_at": "2026-10-20T09:00:00.000Z", "account": BOB,
            "content": "<p>Salut &amp; bienvenue</p><p>ligne<br>deux <a href=\"x\">#otto</a></p>",
            "url": "https://piaille.fr/@bob/111", "replies_count": 2, "favourites_count": 5,
            "reblogs_count": 1, "in_reply_to_id": "100",
            "media_attachments": [{"type": "image", "url": "https://i/1.png", "description": "un chat"},
                                  {"type": "gifv", "url": "https://i/2.mp4", "description": None}]}
BOOST = {"id": "222", "created_at": "2026-10-21T10:00:00.000Z", "account": ANA,
         "content": "", "reblog": ORIGINAL}
OWN = {"id": "333", "created_at": "2026-10-19T08:00:00.000Z", "account": ANA,
       "content": "<p>vieux</p>", "url": "https://mastodon.social/@ana/333"}


def call(name, arguments):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    return r["result"]


def text(r):
    return r["content"][0]["text"]


class Base(unittest.TestCase):
    config = ONE

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        if self.config is not None:
            os.makedirs(os.path.join(d.name, "otto"))
            with open(os.path.join(d.name, "otto", "social.toml"), "w") as f:
                f.write(self.config)
        self.secrets = []

        def daemon(method, params, timeout=10):
            self.assertEqual(method, "secrets.get")
            self.secrets.append(params["name"])
            return {"value": "t0ken-" + params["name"]}
        for p in (mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d.name}),
                  mock.patch.object(server, "daemon", daemon),
                  mock.patch.object(server, "_tokens", {}),
                  mock.patch("time.sleep", lambda s: None)):
            p.start()
            self.addCleanup(p.stop)

    def run_with(self, answers, name, args):
        i = Instances(answers)
        with mock.patch("urllib.request.urlopen", i):
            return call(name, args), i


class Config(Base):
    config = None

    def test_missing_file_gives_the_setup(self):
        r, i = self.run_with({}, "feed", {})
        self.assertTrue(r["isError"])
        self.assertIn("set-key social-<nom>", text(r))
        self.assertIn("[mastodon.<nom>]", text(r))
        self.assertEqual(i.requests, [])

    def test_only_mastodon_tables_with_https(self):
        os.makedirs(os.path.dirname(server.config_path()))
        with open(server.config_path(), "w") as f:
            f.write(TWO + '[mastodon.http]\ninstance = "http://x"\n')
        cfg = server.load_config()
        self.assertEqual(sorted(cfg), ["boulot", "perso"])
        self.assertEqual(cfg["perso"]["instance"], M)
        self.assertEqual(cfg["boulot"]["handle"], "@jb@fosstodon.org")


class Reading(Base):
    def test_feed_maps_reblogs_html_and_media(self):
        r, i = self.run_with({("GET", M + "/api/v1/timelines/home"): [BOOST, OWN]},
                             "feed", {"limit": 500, "since": "2026-10-20"})
        self.assertFalse(r["isError"], text(r))
        [p] = r["structuredContent"]["posts"]  # OWN is before `since`
        self.assertEqual(p["id"], "perso/111")
        self.assertEqual((p["author"], p["handle"]), ("bob", "@bob@piaille.fr"))
        self.assertEqual((p["reposted_by"], p["reposted_handle"]), ("Ana", "@ana@mastodon.social"))
        self.assertEqual(p["text"], "Salut & bienvenue\n\nligne\ndeux #otto")
        self.assertEqual(p["media"], [{"kind": "image", "url": "https://i/1.png", "alt": "un chat"},
                                      {"kind": "video", "url": "https://i/2.mp4", "alt": ""}])
        self.assertEqual((p["reply_to"], p["likes"], p["network"]), ("perso/100", 5, "mastodon"))
        req = i.requests[0]
        self.assertIn("limit=40", req.full_url)
        self.assertEqual(req.get_header("Authorization"), "Bearer t0ken-social-perso")
        self.assertEqual(self.secrets, ["social-perso"])

    def test_notification_kinds(self):
        rows = [{"id": "9", "type": "mention", "created_at": "t", "account": BOB,
                 "status": {**OWN, "in_reply_to_id": "1"}},
                {"id": "8", "type": "mention", "created_at": "t", "account": BOB, "status": OWN},
                {"id": "7", "type": "reblog", "created_at": "t", "account": BOB, "status": OWN},
                {"id": "6", "type": "favourite", "created_at": "t", "account": BOB, "status": OWN},
                {"id": "5", "type": "follow", "created_at": "t", "account": BOB},
                {"id": "4", "type": "poll", "created_at": "t", "account": BOB, "status": OWN},
                {"id": "3", "type": "update", "created_at": "t", "account": BOB, "status": OWN}]
        r, _ = self.run_with({("GET", M + "/api/v1/notifications"): rows,
                              ("GET", M + "/api/v1/markers"): {"notifications": {"last_read_id": "5"}}},
                             "notifications", {"unread": True})
        items = r["structuredContent"]["items"]
        self.assertEqual([x["kind"] for x in items], ["reply", "mention", "repost", "like"])
        self.assertEqual((items[0]["id"], items[0]["post_id"], items[0]["text"]),
                         ("perso/n9", "perso/333", "vieux"))
        r, _ = self.run_with({("GET", M + "/api/v1/notifications"): rows}, "notifications", {})
        self.assertEqual([x["kind"] for x in r["structuredContent"]["items"]][4:],
                         ["follow", "other", "other"])
        self.assertNotIn("post_id", r["structuredContent"]["items"][4])

    def test_tag_and_word_search_routes(self):
        r, i = self.run_with({("GET", M + "/api/v1/timelines/tag/caf%C3%A9"): [OWN]},
                             "search", {"query": "#café"})
        self.assertEqual(r["structuredContent"]["posts"][0]["id"], "perso/333")
        r, i = self.run_with({("GET", M + "/api/v2/search"): {"statuses": [OWN]}},
                             "search", {"query": "logiciel libre"})
        self.assertIn("type=statuses", i.requests[0].full_url)
        self.assertIn("q=logiciel+libre", i.requests[0].full_url)

    def test_thread(self):
        r, _ = self.run_with({("GET", M + "/api/v1/statuses/111"): ORIGINAL,
                              ("GET", M + "/api/v1/statuses/111/context"):
                                  {"ancestors": [OWN], "descendants": []}},
                             "thread", {"id": "perso/111"})
        out = r["structuredContent"]
        self.assertEqual((out["post"]["id"], out["ancestors"][0]["id"], out["replies"]),
                         ("perso/111", "perso/333", []))

    def test_401_gives_the_setup_without_the_token(self):
        r, _ = self.run_with({("GET", M + "/api/v1/accounts/verify_credentials"): http_error(401)},
                             "accounts", {})
        self.assertTrue(r["isError"])
        self.assertIn("set-key social-<nom>", text(r))
        self.assertNotIn("t0ken", text(r))

    def test_retry_once_after_429(self):
        me = {"acct": "ana", "url": "https://mastodon.social/@ana"}
        r, i = self.run_with({("GET", M + "/api/v1/accounts/verify_credentials"):
                              Then(http_error(429, headers={"Retry-After": "0"}), me)}, "accounts", {})
        self.assertEqual(len(i.requests), 2)
        self.assertEqual(r["structuredContent"]["accounts"],
                         [{"id": "perso", "account": "perso", "network": "mastodon",
                           "handle": "@ana@mastodon.social", "url": "https://mastodon.social/@ana"}])


class Several(Base):
    config = TWO

    def test_a_failing_account_is_an_error_line(self):
        F = "https://fosstodon.org"
        r, _ = self.run_with({("GET", M + "/api/v1/timelines/home"): [OWN],
                              ("GET", F + "/api/v1/timelines/home"): http_error(500)}, "feed", {})
        out = r["structuredContent"]
        self.assertEqual([p["id"] for p in out["posts"]], ["perso/333"])
        self.assertIn("boulot", out["errors"][0])

    def test_post_needs_an_account_or_reply_to(self):
        r, i = self.run_with({}, "post", {"text": "bonjour"})
        self.assertTrue(r["isError"])
        self.assertIn("lequel", text(r))
        self.assertEqual(i.requests, [])
        r, i = self.run_with({}, "post", {"text": "  ", "account": "perso"})
        self.assertIn("vide", text(r))
        r, i = self.run_with({}, "post", {"text": "x", "reply_to": "inconnu/1"})
        self.assertIn("inconnu", text(r))
        self.assertEqual(i.requests, [])

    def test_reply_uses_the_account_of_the_post(self):
        F = "https://fosstodon.org"
        r, i = self.run_with({("POST", F + "/api/v1/statuses"): {"id": "42", "url": F + "/@jb/42"}},
                             "post", {"text": "merci !", "reply_to": "boulot/111"})
        self.assertEqual(r["structuredContent"], {"id": "boulot/42", "url": F + "/@jb/42"})
        self.assertEqual(json.loads(i.requests[0].data), {"status": "merci !", "in_reply_to_id": "111"})
        self.assertEqual(i.requests[0].get_header("Authorization"), "Bearer t0ken-social-boulot")

    def test_too_long_surfaces_the_instance_message(self):
        err = http_error(422, b'{"error": "Validation failed: Text character limit of 500 exceeded"}')
        r, _ = self.run_with({("POST", M + "/api/v1/statuses"): err},
                             "post", {"text": "x" * 600, "account": "perso"})
        self.assertIn("character limit of 500", text(r))

    def test_like_and_undo(self):
        r, i = self.run_with({("POST", M + "/api/v1/statuses/111/favourite"): {"favourited": True}},
                             "like", {"id": "perso/111"})
        self.assertEqual(r["structuredContent"], {"id": "perso/111", "liked": True})
        r, i = self.run_with({("POST", M + "/api/v1/statuses/111/unfavourite"): {"favourited": False}},
                             "like", {"id": "perso/111", "undo": True})
        self.assertEqual(r["structuredContent"], {"id": "perso/111", "liked": False})


if __name__ == "__main__":
    unittest.main()
