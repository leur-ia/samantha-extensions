"""cd extensions/bluesky && python3 -m unittest -q"""

import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

import server

PDS = "https://bsky.social/xrpc/"
ME = "did:plc:me"
P1 = "at://did:plc:ana/app.bsky.feed.post/3kaaa"
P2 = "at://did:plc:bob/app.bsky.feed.post/3kbbb"
ROOT = "at://did:plc:cid/app.bsky.feed.post/3kroot"
SESSION = {"accessJwt": "acc1", "refreshJwt": "ref1", "did": ME, "handle": "moi.bsky.social"}


def err(code, error=""):
    return urllib.error.HTTPError("u", code, "no", {}, io.BytesIO(json.dumps({"error": error}).encode()))


class Bsky:
    """Stands in for urlopen: answers an XRPC method from a table (a list is served in
    turn), records requests."""

    def __init__(self, answers):
        self.answers, self.requests = answers, []

    def __call__(self, req, timeout=None):
        assert timeout
        self.requests.append(req)
        answer = self.answers[req.full_url.removeprefix(PDS).split("?")[0]]
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(json.dumps(answer).encode())

    def sent(self, nsid):
        return [json.loads(r.data) if r.data else r.full_url for r in self.requests
                if r.full_url.removeprefix(PDS).split("?")[0] == nsid]


def view(uri, text, handle="ana.bsky.social", **extra):
    return {"uri": uri, "cid": "c-" + uri[-3:], "author": {"handle": handle, "displayName": handle[:3].title()},
            "record": {"text": text, "createdAt": "2026-10-25T10:00:00.000Z"},
            "indexedAt": "2026-10-25T10:00:01.000Z", "likeCount": 2, **extra}


def call(name, arguments):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    return r["result"]


class Tools(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.conf = os.path.join(d.name, "social.toml")
        self.write('[bluesky.moi]\nhandle = "@moi.bsky.social"\n[mastodon.moi]\nserver = "x"\n')
        self.secrets = []
        server._sessions.clear()
        for p in (mock.patch.dict(os.environ, {"SOCIAL_CONFIG": self.conf}),
                  mock.patch.object(server, "daemon", self.daemon),
                  mock.patch.object(server.time, "sleep", lambda s: None)):
            p.start()
            self.addCleanup(p.stop)

    def write(self, text):
        with open(self.conf, "w") as f:
            f.write(text)

    def daemon(self, method, params):
        self.secrets.append((method, params))
        return {"value": "app-pass"}

    def run_with(self, answers, name, args):
        answers.setdefault("com.atproto.server.createSession", dict(SESSION))
        b = Bsky(answers)
        with mock.patch("urllib.request.urlopen", b):
            return call(name, args), b

    def test_missing_config_says_how_to_set_up(self):
        os.remove(self.conf)
        r, b = self.run_with({}, "feed", {})
        text = r["content"][0]["text"]
        self.assertTrue(r["isError"])
        for part in ("social.toml", "Mots de passe d'application", "otto provider set-key social-"):
            self.assertIn(part, text)
        self.assertEqual(b.requests, [])

    def test_session_created_once_and_reused(self):
        b = Bsky({"com.atproto.server.createSession": dict(SESSION),
                  "app.bsky.feed.getTimeline": {"feed": []}})
        with mock.patch("urllib.request.urlopen", b):
            call("feed", {})
            call("feed", {})
        self.assertEqual(b.sent("com.atproto.server.createSession"),
                         [{"identifier": "moi.bsky.social", "password": "app-pass"}])
        self.assertEqual(self.secrets, [("secrets.get", {"name": "social-moi"})])
        self.assertEqual(b.requests[-1].get_header("Authorization"), "Bearer acc1")

    def test_expired_token_is_refreshed(self):
        r, b = self.run_with({"app.bsky.feed.getTimeline": [err(400, "ExpiredToken"), {"feed": []}],
                              "com.atproto.server.refreshSession": dict(SESSION, accessJwt="acc2")}, "feed", {})
        self.assertFalse(r["isError"], r)
        refresh = b.requests[2]  # after createSession and the expired call
        self.assertEqual((refresh.get_method(), refresh.get_header("Authorization")), ("POST", "Bearer ref1"))
        self.assertEqual(b.requests[-1].get_header("Authorization"), "Bearer acc2")

    def test_timeline_with_repost_and_images(self):
        reply = view(P2, "oui !", "bob.bsky.social")
        reply["record"]["reply"] = {"root": {"uri": ROOT, "cid": "c-root"}, "parent": {"uri": P1, "cid": "c-aaa"}}
        pic = view(P1, "regardez", embed={"$type": "app.bsky.embed.images#view",
                                          "images": [{"fullsize": "https://cdn/full.jpg", "thumb": "t", "alt": "un chat"}]})
        timeline = {"feed": [{"post": reply},
                             {"post": pic, "reason": {"$type": "app.bsky.feed.defs#reasonRepost",
                                                      "by": {"handle": "zoe.bsky.social", "displayName": "Zoé"},
                                                      "indexedAt": "2026-10-25T12:00:00.000Z"}}]}
        r, b = self.run_with({"app.bsky.feed.getTimeline": timeline}, "feed", {"limit": 500})
        posts = r["structuredContent"]["posts"]
        self.assertIn("limit=100", b.requests[-1].full_url)
        first, second = posts
        self.assertEqual((first["reposted_by"], first["time"], first["text"]), ("Zoé", "2026-10-25T12:00:00.000Z", "regardez"))
        self.assertEqual(first["media"], [{"kind": "image", "url": "https://cdn/full.jpg", "alt": "un chat"}])
        self.assertEqual(first["id"], f"moi|{P1}|c-aaa")
        self.assertEqual(first["url"], "https://bsky.app/profile/ana.bsky.social/post/3kaaa")
        self.assertEqual((first["network"], first["account"], first["likes"]), ("bluesky", "moi", 2))
        self.assertEqual(second["reply_to"], f"moi|{P1}|c-aaa")
        r, _ = self.run_with({"app.bsky.feed.getTimeline": timeline}, "feed", {"since": "2026-10-25T11:00:00+00:00"})
        self.assertEqual([p["text"] for p in r["structuredContent"]["posts"]], ["regardez"])

    def test_notification_kinds_and_unread(self):
        notes = {"notifications": [
            {"uri": "at://did:plc:ana/app.bsky.feed.like/1", "cid": "l1", "reason": "like", "reasonSubject": P1,
             "author": {"handle": "ana.bsky.social"}, "record": {}, "isRead": True, "indexedAt": "2026-10-25T09:00:00Z"},
            {"uri": P2, "cid": "c-bbb", "reason": "reply", "author": {"handle": "bob.bsky.social", "displayName": "Bob"},
             "record": {"text": "d'accord"}, "isRead": False, "indexedAt": "2026-10-25T10:00:00Z"},
            {"uri": "at://x/q/1", "cid": "q1", "reason": "quote", "author": {"handle": "c"}, "record": {"text": "q"},
             "isRead": False, "indexedAt": "2026-10-25T08:00:00Z"},
            {"uri": "at://x/f/1", "cid": "f1", "reason": "follow", "author": {"handle": "d"}, "record": {},
             "isRead": False, "indexedAt": "2026-10-25T07:00:00Z"}]}
        r, _ = self.run_with({"app.bsky.notification.listNotifications": notes}, "notifications", {})
        items = r["structuredContent"]["items"]
        self.assertEqual([i["kind"] for i in items], ["reply", "like", "other", "follow"])
        self.assertEqual((items[0]["from"], items[0]["text"], items[0]["post_id"]), ("Bob", "d'accord", f"moi|{P2}|c-bbb"))
        self.assertEqual(items[1]["post_id"], f"moi|{P1}|")
        r, _ = self.run_with({"app.bsky.notification.listNotifications": notes}, "notifications", {"unread": True})
        self.assertNotIn("like", [i["kind"] for i in r["structuredContent"]["items"]])

    def test_notification_events_seeded_then_new_only(self):
        old = {"notifications": [{"uri": P1, "cid": "c-aaa", "reason": "mention", "author": {"handle": "ana.bsky.social"},
                                  "record": {"text": "vieux"}, "indexedAt": "2026-10-25T07:00:00Z"}]}
        new = {"notifications": [
            {"uri": "at://did:plc:ana/app.bsky.feed.like/1", "cid": "l1", "reason": "like", "reasonSubject": P1,
             "author": {"handle": "ana.bsky.social"}, "record": {}, "indexedAt": "2026-10-25T09:00:00Z"},
            {"uri": P2, "cid": "c-bbb", "reason": "reply", "author": {"handle": "bob.bsky.social", "displayName": "Bob"},
             "record": {"text": "d'accord " * 40}, "indexedAt": "2026-10-25T08:00:00Z"}] + old["notifications"]}
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        events = []
        b = Bsky({"com.atproto.server.createSession": dict(SESSION),
                  "app.bsky.notification.listNotifications": [old, new, new]})
        with mock.patch.dict(os.environ, {"BLUESKY_STATE": os.path.join(d.name, "b", "n.json")}), \
                mock.patch("urllib.request.urlopen", b):
            for _ in range(3):
                server.check_notifications(events.append)
        self.assertEqual(len(events), 1, "the first check seeds; likes aren't news; nothing twice")
        e = events[0]
        self.assertEqual((e["kind"], e["network"], e["account"], e["from"], e["id"], e["date"]),
                         ("reply", "bluesky", "moi", "Bob", f"moi|{P2}|c-bbb", "2026-10-25T08:00:00Z"))
        self.assertEqual((len(e["text"]), e["url"]), (200, "https://bsky.app/profile/did:plc:bob/post/3kbbb"))

    def test_search(self):
        r, b = self.run_with({"app.bsky.feed.searchPosts": {"posts": [view(P1, "#otto c'est bien")]}},
                             "search", {"query": "#otto", "limit": 5})
        self.assertEqual(r["structuredContent"]["posts"][0]["text"], "#otto c'est bien")
        url = b.requests[-1].full_url
        for part in ("q=%23otto", "sort=latest", "limit=5"):
            self.assertIn(part, url)

    def test_thread(self):
        t = {"thread": {"post": view(P2, "milieu"),
                        "parent": {"post": view(P1, "haut"), "parent": {"post": view(ROOT, "racine")}},
                        "replies": [{"post": view("at://x/app.bsky.feed.post/r1", "réponse"),
                                     "replies": [{"post": view("at://x/app.bsky.feed.post/r2", "sous-réponse")}]},
                                    {"$type": "app.bsky.feed.defs#blockedPost", "uri": "at://y"}]}}
        r, b = self.run_with({"app.bsky.feed.getPostThread": t}, "thread", {"id": f"bluesky:moi|{P2}|c-bbb"})
        out = r["structuredContent"]
        self.assertEqual(out["post"]["text"], "milieu")
        self.assertEqual([p["text"] for p in out["ancestors"]], ["racine", "haut"])
        self.assertEqual([p["text"] for p in out["replies"]], ["réponse", "sous-réponse"])
        self.assertTrue(call("thread", {"id": "moi|https://evil|x"})["isError"])

    def test_post_and_reply_resolve_root_and_parent(self):
        made = {"uri": f"at://{ME}/app.bsky.feed.post/3knew", "cid": "c-new"}
        r, b = self.run_with({"com.atproto.repo.createRecord": made}, "post", {"text": "Bonjour"})
        self.assertEqual(r["structuredContent"], {"id": f"moi|{made['uri']}|c-new",
                                                  "url": "https://bsky.app/profile/moi.bsky.social/post/3knew"})
        body = b.sent("com.atproto.repo.createRecord")[0]
        self.assertEqual((body["repo"], body["collection"], body["record"]["text"]), (ME, "app.bsky.feed.post", "Bonjour"))
        self.assertNotIn("reply", body["record"])
        # A reply to a reply: root comes from the parent's record.
        parent = view(P2, "oui")
        parent["record"]["reply"] = {"root": {"uri": ROOT, "cid": "c-root"}, "parent": {"uri": P1, "cid": "c-aaa"}}
        r, b = self.run_with({"app.bsky.feed.getPosts": {"posts": [parent]}, "com.atproto.repo.createRecord": made},
                             "post", {"text": "et moi", "reply_to": f"moi|{P2}|"})
        reply = b.sent("com.atproto.repo.createRecord")[0]["record"]["reply"]
        self.assertEqual(reply, {"root": {"uri": ROOT, "cid": "c-root"}, "parent": {"uri": P2, "cid": "c-bbb"}})
        # A reply to a top post: it is its own root.
        r, b = self.run_with({"app.bsky.feed.getPosts": {"posts": [view(P1, "haut")]},
                              "com.atproto.repo.createRecord": made}, "post", {"text": "ok", "reply_to": f"moi|{P1}|c-aaa"})
        reply = b.sent("com.atproto.repo.createRecord")[0]["record"]["reply"]
        self.assertEqual(reply["root"], reply["parent"])
        self.assertEqual(reply["root"], {"uri": P1, "cid": "c-aaa"})

    def test_post_limit_is_300_graphemes(self):
        self.assertEqual(server.graphemes("é" + "é" + "👍🏽" + "👨‍👩‍👧" + "🇫🇷"), 5)
        r, b = self.run_with({}, "post", {"text": "a" * 301})
        self.assertIn("300", r["content"][0]["text"])
        self.assertEqual(b.requests, [], "nothing sent")
        made = {"uri": f"at://{ME}/app.bsky.feed.post/3k", "cid": "c"}
        r, _ = self.run_with({"com.atproto.repo.createRecord": made}, "post", {"text": "🇫🇷" * 300})
        self.assertFalse(r["isError"], r)

    def test_post_needs_account_when_several(self):
        self.write('[bluesky.moi]\nhandle = "moi.bsky.social"\n[bluesky.pro]\nhandle = "pro.example"\n')
        r, b = self.run_with({}, "post", {"text": "x"})
        self.assertIn("précise account", r["content"][0]["text"])
        self.assertEqual(b.requests, [])

    def test_like_and_undo(self):
        r, b = self.run_with({"com.atproto.repo.createRecord": {"uri": f"at://{ME}/app.bsky.feed.like/3l", "cid": "x"}},
                             "like", {"id": f"moi|{P1}|c-aaa"})
        self.assertEqual(r["structuredContent"], {"id": f"moi|{P1}|c-aaa", "liked": True})
        body = b.sent("com.atproto.repo.createRecord")[0]
        self.assertEqual((body["collection"], body["record"]["subject"]), ("app.bsky.feed.like", {"uri": P1, "cid": "c-aaa"}))
        # From a notification's post_id, without a cid: looked up first.
        r, b = self.run_with({"app.bsky.feed.getPosts": {"posts": [view(P1, "x")]},
                              "com.atproto.repo.createRecord": {"uri": "u", "cid": "x"}}, "like", {"id": f"moi|{P1}|"})
        self.assertEqual(b.sent("com.atproto.repo.createRecord")[0]["record"]["subject"]["cid"], "c-aaa")
        liked = view(P1, "x", viewer={"like": f"at://{ME}/app.bsky.feed.like/3lmine"})
        r, b = self.run_with({"app.bsky.feed.getPosts": {"posts": [liked]}, "com.atproto.repo.deleteRecord": {}},
                             "like", {"id": f"moi|{P1}|c-aaa", "undo": True})
        self.assertEqual(r["structuredContent"]["liked"], False)
        self.assertEqual(b.sent("com.atproto.repo.deleteRecord"),
                         [{"repo": ME, "collection": "app.bsky.feed.like", "rkey": "3lmine"}])

    def test_rejected_app_password_says_how_to_set_up(self):
        r, b = self.run_with({"com.atproto.server.createSession": err(401, "AuthenticationRequired")}, "feed", {})
        text = r["content"][0]["text"]
        self.assertTrue(r["isError"])
        self.assertIn("otto provider set-key social-moi", text)
        self.assertNotIn("app-pass", text)

    def test_retry_once_after_429_and_accounts(self):
        r, b = self.run_with({"app.bsky.actor.getProfile": [err(429), {"handle": "moi.bsky.social", "displayName": "Moi"}]},
                             "accounts", {})
        self.assertEqual(r["structuredContent"]["accounts"],
                         [{"id": "moi", "account": "moi", "network": "bluesky", "handle": "moi.bsky.social",
                           "name": "Moi", "url": "https://bsky.app/profile/moi.bsky.social"}])

    def test_protocol(self):
        tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["accounts", "feed", "notifications", "search", "thread", "post", "like"])


if __name__ == "__main__":
    unittest.main()
