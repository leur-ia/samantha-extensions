"""python3 -m unittest discover extensions/github"""

import io
import json
import os
import tempfile
import unittest
import urllib.parse
from unittest import mock

import server


class Gh:
    def __init__(self):
        self.requests = []
        self.notes = None

    def __call__(self, req, timeout=None):
        assert timeout and req.get_header("Authorization") == "Bearer ghp_x"
        u = urllib.parse.urlparse(req.full_url)
        self.requests.append((req.get_method(), u.path, dict(urllib.parse.parse_qsl(u.query))))
        if u.path == "/user":
            body = {"login": "jb"}
        elif u.path == "/notifications" and self.notes is not None:
            body = self.notes.pop(0)
        elif u.path == "/notifications":
            body = [{"id": "123", "reason": "review_requested", "updated_at": "2026-10-04T09:00:00Z", "unread": True,
                     "subject": {"title": "Fix", "type": "PullRequest", "url": "https://api.github.com/repos/o/r/pulls/7"},
                     "repository": {"full_name": "o/r"}}]
        elif u.path == "/search/issues":
            body = {"items": [{"number": 7, "title": "Fix", "repository_url": "https://api.github.com/repos/o/r",
                               "user": {"login": "ana"}, "updated_at": "2026-10-04T09:00:00Z", "html_url": "https://github.com/o/r/pull/7"}]}
        elif u.path == "/user/repos":
            body = [{"full_name": "o/r", "pushed_at": "2026-10-04T08:00:00Z", "html_url": "https://github.com/o/r", "private": True}]
        elif u.path == "/repos/o/r/actions/runs":
            body = {"workflow_runs": [{"name": "CI", "status": "completed", "conclusion": "failure", "head_branch": "main",
                                       "updated_at": "2026-10-04T08:30:00Z", "html_url": "https://github.com/o/r/actions/runs/1"}]}
        else:
            body = {}
        return io.BytesIO(json.dumps(body).encode())


def call(name, args):
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})["result"]


class GithubTest(unittest.TestCase):
    def setUp(self):
        self.g = Gh()
        for p in (mock.patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_x"}), mock.patch("urllib.request.urlopen", self.g)):
            p.start()
            self.addCleanup(p.stop)

    def test_inbox_links_to_the_page(self):
        i = call("inbox", {})["structuredContent"]["items"][0]
        self.assertEqual((i["kind"], i["reason"], i["url"]), ("pr", "review_requested", "https://github.com/o/r/pull/7"))

    def test_reviews_mine_repos_ci(self):
        r = call("reviews", {})["structuredContent"]["items"][0]
        self.assertEqual((r["id"], r["repo"], r["author"]), ("o/r#7", "o/r", "ana"))
        self.assertIn("review-requested:@me", self.g.requests[-1][2]["q"])
        call("mine", {})
        self.assertIn("author:@me", self.g.requests[-1][2]["q"])
        self.assertEqual(call("repos", {})["structuredContent"]["repos"][0]["repo"], "o/r")
        self.assertEqual(call("ci", {"repo": "o/r"})["structuredContent"]["runs"][0]["status"], "failure")
        self.assertTrue(call("ci", {"repo": "../etc"})["isError"])
        self.assertTrue(call("ci", {"repo": "o/.."})["isError"])
        self.assertFalse(call("ci", {"repo": "o/.github"})["isError"], "a repo may start with a dot")

    def test_mark_read_and_setup(self):
        call("mark_read", {"id": "123"})
        self.assertEqual(self.g.requests[-1][:2], ("PATCH", "/notifications/threads/123"))
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": ""}):
            self.assertIn("set-key github", call("inbox", {})["content"][0]["text"])

    def test_notification_events_seeded_then_new_only(self):
        def note(nid, reason, updated, kind="PullRequest", title="Fix"):
            return {"id": nid, "reason": reason, "updated_at": updated, "repository": {"full_name": "o/r"},
                    "subject": {"title": title, "type": kind, "url": f"https://api.github.com/repos/o/r/pulls/{nid}"}}
        old = [note("1", "review_requested", "2026-10-04T09:00:00Z")]
        new = [note("3", "subscribed", "2026-10-04T11:00:00Z"),
               note("2", "ci_activity", "2026-10-04T10:30:00Z", "CheckSuite", "CI workflow run failed"),
               note("1", "review_requested", "2026-10-04T10:00:00Z")]  # active again
        self.g.notes = [old, new, new]
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        events = []
        with mock.patch.dict(os.environ, {"GITHUB_STATE": os.path.join(d.name, "g", "n.json")}):
            for _ in range(3):
                server.check_notifications(events.append)
            self.assertEqual(server.load_state()["since"], "2026-10-04T11:00:00Z")
        since = [r[2].get("since") for r in self.g.requests if r[1] == "/notifications"]
        self.assertEqual(since, [None, "2026-10-04T09:00:00Z", "2026-10-04T11:00:00Z"])
        self.assertEqual([(e["id"], e["reason"]) for e in events], [("1", "review_requested"), ("2", "ci_activity")],
                         "seeded first; subscribed isn't news; nothing twice")
        self.assertEqual(events[0], {"account": "jb", "forge": "github", "reason": "review_requested", "kind": "pr",
                                     "repo": "o/r", "title": "Fix", "url": "https://github.com/o/r/pull/1", "id": "1",
                                     "date": "2026-10-04T10:00:00Z"})
        self.assertEqual((events[1]["kind"], events[1]["url"]), ("ci", "https://github.com/o/r"))

    def test_protocol(self):
        self.assertEqual(list(server.TOOLS), ["inbox", "reviews", "mine", "repos", "ci", "mark_read"])


if __name__ == "__main__":
    unittest.main()
