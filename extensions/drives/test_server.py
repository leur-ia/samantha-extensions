"""python3 -m unittest discover contrib/drives"""

import json
import subprocess
import unittest
from unittest import mock

import server

TREE = {"blockdevices": [
    {"name": "nvme0n1", "path": "/dev/nvme0n1", "type": "disk", "rm": False, "hotplug": False, "tran": "nvme", "size": 512e9,
     "children": [{"name": "nvme0n1p3", "path": "/dev/nvme0n1p3", "fstype": "btrfs", "type": "part", "size": 500e9}]},
    {"name": "sda", "path": "/dev/sda", "type": "disk", "rm": True, "hotplug": True, "tran": "usb", "size": 32e9,
     "vendor": "SanDisk ", "model": "Ultra", "children": [
        {"name": "sda1", "path": "/dev/sda1", "label": "PHOTOS", "fstype": "exfat", "type": "part", "size": 31.9e9,
         "mountpoints": [None]}]},
]}


class Udisks:
    def __init__(self):
        self.calls, self.mounted = [], None

    def __call__(self, argv, capture_output, text, timeout, env):
        assert timeout
        self.calls.append(argv)
        if argv[0] == "lsblk":
            tree = json.loads(json.dumps(TREE))
            tree["blockdevices"][1]["children"][0]["mountpoints"] = [self.mounted]
            return subprocess.CompletedProcess(argv, 0, json.dumps(tree), "")
        if argv[1] == "mount":
            self.mounted = "/run/media/jb/PHOTOS"
            return subprocess.CompletedProcess(argv, 0, "Mounted /dev/sda1 at /run/media/jb/PHOTOS\n", "")
        if argv[1] == "unmount":
            self.mounted = None
        return subprocess.CompletedProcess(argv, 0, "", "")


def call(name, arguments):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    return r["result"]


class Drives(unittest.TestCase):
    def setUp(self):
        self.u = Udisks()
        p = mock.patch("subprocess.run", self.u)
        p.start()
        self.addCleanup(p.stop)

    def test_list_only_removable(self):
        d = call("list", {})["structuredContent"]["drives"]
        self.assertEqual(d, [{"disk": "/dev/sda", "name": "SanDisk Ultra", "size": "32.0 Go", "connection": "usb",
                              "partitions": [{"device": "/dev/sda1", "label": "PHOTOS", "size": "31.9 Go",
                                              "filesystem": "exfat", "mounted_at": None}]}])

    def test_mount_then_eject(self):
        self.assertEqual(call("mount", {"drive": "photos"})["structuredContent"]["mounted_at"], "/run/media/jb/PHOTOS")
        self.assertTrue(call("mount", {})["structuredContent"]["already"])
        r = call("eject", {})["structuredContent"]
        self.assertEqual(r, {"ejected": "SanDisk Ultra", "powered_off": True})
        self.assertIn(["udisksctl", "power-off", "-b", "/dev/sda", "--no-user-interaction"], self.u.calls)
        self.assertTrue(call("mount", {"drive": "backup"})["isError"])

    def test_busy(self):
        def busy(argv, **kw):
            if argv[0] == "lsblk":
                return self.u(argv, **kw)
            return subprocess.CompletedProcess(argv, 1, "", "Error unmounting /dev/sda1: target is busy")
        self.u.mounted = "/run/media/jb/PHOTOS"
        with mock.patch("subprocess.run", busy):
            self.assertIn("encore utilisé", call("eject", {})["content"][0]["text"])

    def test_plug_changes(self):
        empty, one = [], call("list", {})["structuredContent"]["drives"]
        self.assertEqual(server.changes(empty, one), (one, []))
        self.assertEqual(server.changes(one, empty), ([], one))

    def test_protocol(self):
        tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["list", "mount", "unmount", "eject"])


if __name__ == "__main__":
    unittest.main()
