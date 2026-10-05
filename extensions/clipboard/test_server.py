"""python3 -m unittest discover extensions/clipboard"""

import json
import os
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

import server


def call(name, args):
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})["result"]


class Clipboard(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.clock = [1791100000.0]
        for p in (mock.patch.object(server, "DIR", d.name), mock.patch.object(server, "HISTORY", os.path.join(d.name, "h.jsonl")),
                  mock.patch.object(server, "now", lambda: self.clock[0])):
            p.start()
            self.addCleanup(p.stop)

    def copy(self, text, types=("text/plain",)):
        self.clock[0] += 1
        return server.keep(text, list(types))

    def test_keeps_text_skips_secrets_and_repeats(self):
        self.assertTrue(self.copy("https://example.org/a"))
        self.assertFalse(self.copy("hunter2", ("text/plain", "x-kde-passwordManagerHint")), "a password")
        self.assertFalse(self.copy("https://example.org/a"), "the same again")
        self.assertFalse(self.copy("   "))
        self.assertTrue(self.copy("Bonjour à tous"))
        self.assertEqual(stat.S_IMODE(os.stat(server.HISTORY).st_mode), 0o600)
        e = call("history", {})["structuredContent"]["entries"]
        self.assertEqual([(x["index"], x["text"]) for x in e], [(0, "Bonjour à tous"), (1, "https://example.org/a")])
        self.assertEqual(call("history", {"search": "HTTP"})["structuredContent"]["entries"][0]["index"], 1)

    def test_expiry_and_cap(self):
        self.copy("old")
        self.clock[0] += 4 * 86400
        self.copy("new")
        self.assertEqual([x["text"] for x in call("history", {})["structuredContent"]["entries"]], ["new"])
        for i in range(250):
            self.copy(f"c{i}")
        self.assertEqual(len(server.load()), server.KEEP)

    def test_get_copy_clear(self):
        self.copy("x" * 6000)
        g = call("get", {"index": 0})["structuredContent"]
        self.assertEqual((len(g["text"]), g["truncated"]), (5000, True))
        self.assertTrue(call("get", {"index": 9})["isError"])
        with mock.patch("subprocess.run") as run:
            call("copy", {"index": 0})
        self.assertEqual(run.call_args.args[0], ["wl-copy"])
        self.assertEqual(len(run.call_args.kwargs["input"]), 5000)
        self.assertEqual(call("clear", {})["structuredContent"]["cleared"], 1)
        self.assertEqual(server.load(), [])

    def test_protocol(self):
        self.assertEqual(list(server.TOOLS), ["history", "get", "copy", "clear"])


if __name__ == "__main__":
    unittest.main()
