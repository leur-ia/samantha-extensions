"""python3 -m unittest discover extensions/gitlab"""

import io
import json
import os
import unittest
import urllib.parse
from unittest import mock

import server


class Gl:
    def __init__(self):
        self.requests = []

    def __call__(self, req, timeout=None):
        assert timeout and req.get_header("Private-token") == "glpat-x"
        u = urllib.parse.urlparse(req.full_url)
        self.requests.append((req.get_method(), u.path, dict(urllib.parse.parse_qsl(u.query))))
        p = u.path.removeprefix("/api/v4")
        if p == "/todos":
            body = [{"id": 9, "target_type": "MergeRequest", "action_name": "review_requested", "target": {"title": "Fix"},
                     "project": {"path_with_namespace": "g/p"}, "author": {"username": "ana"},
                     "updated_at": "2026-10-04T09:00:00Z", "target_url": "https://gitlab.com/g/p/-/merge_requests/3"}]
        elif p == "/user":
            body = {"username": "me"}
        elif p == "/merge_requests":
            body = [{"iid": 3, "title": "Fix", "references": {"full": "g/p!3"}, "author": {"username": "ana"},
                     "updated_at": "2026-10-04T09:00:00Z", "web_url": "https://gitlab.com/g/p/-/merge_requests/3"}]
        elif p == "/projects":
            body = [{"path_with_namespace": "g/p", "last_activity_at": "2026-10-04T08:00:00Z", "web_url": "u", "visibility": "private"}]
        elif p == "/projects/g%2Fp/pipelines":
            body = [{"iid": 92, "status": "failed", "ref": "main", "updated_at": "2026-10-04T08:30:00Z", "web_url": "u"}]
        else:
            body = {}
        return io.BytesIO(json.dumps(body).encode())


def call(name, args):
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})["result"]


class GitlabTest(unittest.TestCase):
    def setUp(self):
        self.g = Gl()
        server._me.clear()
        for p in (mock.patch.dict(os.environ, {"GITLAB_TOKEN": "glpat-x", "XDG_CONFIG_HOME": "/nonexistent"}),
                  mock.patch("urllib.request.urlopen", self.g)):
            p.start()
            self.addCleanup(p.stop)

    def test_todos_reviews_mine(self):
        i = call("inbox", {})["structuredContent"]["items"][0]
        self.assertEqual((i["id"], i["kind"], i["repo"], i["reason"]), ("9", "mr", "g/p", "review_requested"))
        r = call("reviews", {})["structuredContent"]["items"][0]
        self.assertEqual((r["id"], r["repo"]), ("g/p!3", "g/p"))
        self.assertEqual(self.g.requests[-1][2]["reviewer_username"], "me")
        call("mine", {})
        self.assertEqual(self.g.requests[-1][2]["scope"], "created_by_me")

    def test_repos_ci_mark(self):
        self.assertEqual(call("repos", {})["structuredContent"]["repos"][0]["private"], True)
        self.assertEqual(call("ci", {"repo": "g/p"})["structuredContent"]["runs"][0]["status"], "failed")
        self.assertTrue(call("ci", {"repo": "p"})["isError"])
        self.assertTrue(call("ci", {"repo": "../p"})["isError"])
        call("mark_read", {"id": "9"})
        self.assertEqual(self.g.requests[-1][:2], ("POST", "/api/v4/todos/9/mark_as_done"))

    def test_setup(self):
        with mock.patch.dict(os.environ, {"GITLAB_TOKEN": ""}):
            self.assertIn("set-key gitlab", call("inbox", {})["content"][0]["text"])

    def test_protocol(self):
        self.assertEqual(list(server.TOOLS), ["inbox", "reviews", "mine", "repos", "ci", "mark_read"])


if __name__ == "__main__":
    unittest.main()
