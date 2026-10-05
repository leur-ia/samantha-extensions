"""python3 -m unittest discover extensions/rclone"""

import json
import subprocess
import tempfile
import unittest
from unittest import mock

import server


class Rclone:
    def __init__(self, remotes=None):
        self.calls = []
        self.remotes = {"gdrive": {"type": "drive"}, "kdrive": {"type": "webdav"}} if remotes is None else remotes

    def __call__(self, argv, capture_output, text, timeout):
        assert timeout
        self.calls.append(argv[1:])
        if argv[1:3] == ["config", "dump"]:
            out = json.dumps(self.remotes)
        elif argv[1] == "lsjson" and "--recursive" in argv:
            if argv[2] == "kdrive:":
                return subprocess.CompletedProcess(argv, 1, "", "Failed to lsjson: 401 Unauthorized")
            out = json.dumps([{"Path": "Maison/Devis cuisine.pdf", "Name": "Devis cuisine.pdf", "Size": 120000, "ModTime": "2026-09-30T10:00:00Z"}])
        elif argv[1] == "lsjson":
            out = json.dumps([{"Name": "b.txt", "Path": "b.txt", "Size": 3, "IsDir": False, "ModTime": "2026-10-01T00:00:00Z"},
                              {"Name": "Maison", "Path": "Maison", "IsDir": True, "Size": -1, "ModTime": "2026-09-01T00:00:00Z"}])
        else:
            out = ""
        return subprocess.CompletedProcess(argv, 0, out, "")


def call(name, args):
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})["result"]


class RcloneTest(unittest.TestCase):
    def test_places_and_list(self):
        with mock.patch("subprocess.run", Rclone()):
            self.assertEqual(call("places", {})["structuredContent"]["places"][0], {"place": "gdrive", "name": "gdrive", "kind": "drive"})
            e = call("list", {"place": "gdrive"})["structuredContent"]["entries"]
        self.assertEqual([(x["name"], x["dir"]) for x in e], [("Maison", True), ("b.txt", False)], "folders first")

    def test_search_every_remote_keeps_going(self):
        r = Rclone()
        with mock.patch("subprocess.run", r):
            out = call("search", {"query": "devis cuisine"})["structuredContent"]
        self.assertEqual(out["results"][0]["path"], "Maison/Devis cuisine.pdf")
        self.assertIn("kdrive", out["errors"][0])
        self.assertIn("*devis*cuisine*", r.calls[1])

    def test_download_upload_and_paths(self):
        r = Rclone()
        with mock.patch("subprocess.run", r), tempfile.NamedTemporaryFile() as f, tempfile.TemporaryDirectory() as d, \
             mock.patch.object(server, "DOWNLOADS", d):
            out = call("download", {"place": "gdrive", "path": "Maison/Devis cuisine.pdf"})["structuredContent"]
            self.assertTrue(out["downloaded"].endswith("/Devis cuisine.pdf"))
            call("upload", {"place": "gdrive", "file": f.name, "to": "Inbox"})
            self.assertEqual(r.calls[-1][:2], ["copy", f.name])
            self.assertTrue(call("download", {"place": "gdrive", "path": "../../etc"})["isError"])
            self.assertTrue(call("upload", {"place": "gdrive", "file": "relative.txt"})["isError"])
            self.assertTrue(call("list", {"place": "gd;rm"})["isError"])

    def test_no_remote(self):
        with mock.patch("subprocess.run", Rclone(remotes={})):
            self.assertIn("rclone config", call("places", {})["content"][0]["text"])

    def test_protocol(self):
        self.assertEqual(list(server.TOOLS), ["places", "list", "search", "download", "upload"])


if __name__ == "__main__":
    unittest.main()
