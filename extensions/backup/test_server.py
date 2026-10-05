"""python3 -m unittest discover extensions/backup"""

import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import server

SNAPS = [{"short_id": "aaaa1111", "id": "aaaa1111ffff", "time": "2026-10-03T23:00:05+02:00", "paths": ["/home/jb/Documents"], "hostname": "fedora8"},
         {"short_id": "bbbb2222", "id": "bbbb2222ffff", "time": "2026-10-04T23:00:05+02:00", "paths": ["/home/jb/Documents"], "hostname": "fedora8"}]


class Restic:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self, argv, capture_output, text, timeout, env):
        assert timeout and env["RESTIC_REPOSITORY"] == "/run/media/jb/Disque/restic" and env["RESTIC_PASSWORD"] == "pw"
        self.calls.append(argv[1:])
        if self.fail:
            return subprocess.CompletedProcess(argv, 1, "", self.fail)
        cmd = argv[1]
        if cmd == "snapshots":
            out = json.dumps(SNAPS[-1:] if "--latest" in argv else SNAPS)
        elif cmd == "backup":
            out = '{"message_type":"status"}\n{"message_type":"summary","snapshot_id":"cccc3333dddd","files_new":3,"files_changed":1,"data_added":2500000}'
        elif cmd == "ls":
            out = '{"struct_type":"snapshot"}\n{"struct_type":"node","path":"/home/jb/Documents/cv.pdf","type":"file","size":1200}'
        else:
            out = ""
        return subprocess.CompletedProcess(argv, 0, out, "")


def call(name, args):
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})["result"]


class Backup(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        os.makedirs(os.path.join(d.name, "samantha"))
        with open(os.path.join(d.name, "samantha", "backup.toml"), "w") as f:
            f.write('repository = "/run/media/jb/Disque/restic"\npaths = ["~/Documents"]\nexclude = ["*.tmp"]\nkeep_daily = 7\n')
        self.events = []
        for p in (mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d.name, "RESTIC_PASSWORD": "pw"}),
                  mock.patch.object(server, "daemon_publish", lambda k, data: self.events.append((k, data))),
                  mock.patch.object(server, "RESTORED", os.path.join(d.name, "Restauré"))):
            p.start()
            self.addCleanup(p.stop)
        server._running.update(since=None, last=None)

    def test_status_and_snapshots(self):
        with mock.patch("subprocess.run", Restic()):
            s = call("status", {})["structuredContent"]
            snaps = call("snapshots", {})["structuredContent"]["snapshots"]
        self.assertEqual((s["latest"]["id"], s["latest"]["time"]), ("bbbb2222", "2026-10-04 23:00"))
        self.assertEqual([x["id"] for x in snaps], ["bbbb2222", "aaaa1111"])

    def test_backup_runs_in_background_and_prunes(self):
        r = Restic()
        with mock.patch("subprocess.run", r):
            self.assertTrue(call("backup", {})["structuredContent"]["started"])
            for _ in range(100):
                if server._running["last"]:
                    break
                time.sleep(0.02)
        last = server._running["last"]
        self.assertEqual((last["ok"], last["snapshot"], last["added_mb"]), (True, "cccc3333", 2.5))
        backup_call = next(c for c in r.calls if c[0] == "backup")
        self.assertIn(os.path.expanduser("~/Documents"), backup_call)
        self.assertIn("--exclude", backup_call)
        self.assertIn(["forget", "--prune", "--keep-daily=7"], r.calls)
        kinds = [k for k, _ in self.events]
        self.assertEqual(kinds, ["backup.activity", "backup.activity", "backup.done"])

    def test_files_and_restore_into_a_new_folder(self):
        r = Restic()
        with mock.patch("subprocess.run", r):
            f = call("files", {"path": "~/Documents"})["structuredContent"]
            out = call("restore", {"path": "/home/jb/Documents/cv.pdf"})["structuredContent"]
        self.assertEqual(f["entries"][0]["path"], "/home/jb/Documents/cv.pdf")
        self.assertTrue(out["into"].startswith(server.RESTORED))
        self.assertTrue(call("restore", {"path": "../etc/passwd"})["isError"])
        self.assertTrue(call("files", {"snapshot": "; rm -rf"})["isError"])

    def test_errors(self):
        with mock.patch("subprocess.run", Restic(fail="Fatal: wrong password or no key found")):
            self.assertIn("Mot de passe", call("snapshots", {})["content"][0]["text"])
        with mock.patch.dict(os.environ, {"RESTIC_PASSWORD": ""}):
            self.assertIn("backup-restic", call("status", {})["content"][0]["text"])

    def test_protocol(self):
        self.assertEqual(list(server.TOOLS), ["status", "snapshots", "backup", "files", "restore", "init"])


if __name__ == "__main__":
    unittest.main()
