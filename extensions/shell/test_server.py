"""python3 -m unittest (no network: local commands only)"""

import json
import os
import tempfile
import unittest

import server


def call(name, arguments):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})["result"]
    return r.get("structuredContent"), r


class Shell(unittest.TestCase):
    def test_a_command_runs_where_asked_and_says_how_it_ended(self):
        with tempfile.TemporaryDirectory() as d:
            out, _ = call("run", {"command": "pwd; echo oops >&2; exit 3", "cwd": d})
            self.assertEqual(out["exit"], 3)
            self.assertEqual(os.path.realpath(out["stdout"].strip()), os.path.realpath(d))
            self.assertEqual(out["stderr"].strip(), "oops")

    def test_long_output_is_cut_and_slow_commands_stop(self):
        out, _ = call("run", {"command": "yes x | head -c 50000"})
        self.assertIn("caractères coupés", out["stdout"])
        self.assertLess(len(out["stdout"]), server.OUTPUT + 200)
        out, _ = call("run", {"command": "echo start; sleep 5", "timeout": 1})
        self.assertTrue(out["timed_out"])

    def test_bad_input_is_a_message_not_a_crash(self):
        for args in ({}, {"command": "  "}, {"command": "ls", "cwd": "/no/such/dir"}):
            _, r = call("run", args)
            self.assertTrue(r["isError"], args)
        _, r = call("help", {"program": "ls; rm -rf /"})
        self.assertTrue(r["isError"])
        _, r = call("help", {"program": "surely-not-installed-xyz"})
        self.assertIn("pas installé", r["content"][0]["text"])

    def test_help_and_which(self):
        out, _ = call("help", {"program": "ls"})
        self.assertIn(out["source"], ("man", "tldr", "--help"))
        self.assertTrue(out["text"])
        out, _ = call("which", {"names": ["ls", "surely-not-installed-xyz"]})
        self.assertIn("ls", out["found"])
        self.assertEqual(out["missing"], ["surely-not-installed-xyz"])
        out, _ = call("which", {})
        self.assertIn("bash", out["found"])

    def test_tools_are_listed(self):
        r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]
        self.assertEqual([t["name"] for t in r["tools"]], ["run", "help", "which"])


if __name__ == "__main__":
    unittest.main()
