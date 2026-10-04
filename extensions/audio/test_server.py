"""python3 -m unittest discover contrib/audio"""

import json
import subprocess
import unittest
from unittest import mock

import server

DUMP = [
    {"id": 30, "type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "default"},
     "metadata": [{"key": "default.audio.sink", "value": {"name": "alsa_output.analog"}},
                  {"key": "default.audio.source", "value": {"name": "alsa_input.analog"}}]},
    {"id": 54, "info": {"props": {"media.class": "Audio/Sink", "node.name": "alsa_output.analog",
                                  "node.description": "Built-in Audio"}}},
    {"id": 61, "info": {"props": {"media.class": "Audio/Sink", "node.name": "bluez_output.AA_BB",
                                  "node.description": "WH-1000XM5"}}},
    {"id": 55, "info": {"props": {"media.class": "Audio/Source", "node.name": "alsa_input.analog",
                                  "node.description": "Built-in Mic"}}},
    {"id": 70, "info": {"props": {"media.class": "Video/Source", "node.name": "cam"}}},
]
INFO = {"AA:BB:CC:DD:EE:FF": "Name: WH-1000XM5\n\tConnected: no\n\tUUID: Audio Sink (0000110b)\n",
        "11:22:33:44:55:66": "Name: Mouse\n\tConnected: yes\n"}


class System:
    def __init__(self):
        self.calls, self.volumes = [], {"54": "Volume: 0.40", "61": "Volume: 0.80 [MUTED]",
                                        "55": "Volume: 1.00", "@DEFAULT_AUDIO_SINK@": "Volume: 0.40"}

    def __call__(self, argv, capture_output, text, timeout):
        assert timeout
        self.calls.append(argv)
        out = ""
        if argv[0] == "pw-dump":
            out = json.dumps(DUMP)
        elif argv[:2] == ["wpctl", "get-volume"]:
            out = self.volumes[argv[2]]
        elif argv[:3] == ["bluetoothctl", "devices", "Paired"]:
            out = "Device AA:BB:CC:DD:EE:FF WH-1000XM5\nDevice 11:22:33:44:55:66 Mouse\n"
        elif argv[:2] == ["bluetoothctl", "info"]:
            out = INFO[argv[2]]
        return subprocess.CompletedProcess(argv, 0, out, "")


def call(name, arguments):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    return r["result"]


class Audio(unittest.TestCase):
    def setUp(self):
        self.sys = System()
        p = mock.patch("subprocess.run", self.sys)
        p.start()
        self.addCleanup(p.stop)

    def test_devices(self):
        rows = call("devices", {})["structuredContent"]["devices"]
        self.assertEqual([(d["name"], d["kind"], d["default"], d["volume"], d["muted"], d["bluetooth"]) for d in rows], [
            ("Built-in Audio", "output", True, 40, False, False),
            ("WH-1000XM5", "output", False, 80, True, True),
            ("Built-in Mic", "input", True, 100, False, False)])

    def test_use_by_name(self):
        r = call("use", {"device": "wh-1000"})
        self.assertEqual(r["structuredContent"]["default"], "WH-1000XM5")
        self.assertIn(["wpctl", "set-default", "61"], self.sys.calls)
        self.assertTrue(call("use", {"device": "hdmi"})["isError"])
        self.assertTrue(call("use", {"device": "built-in", "kind": "both"})["isError"])

    def test_volume_set_change_mute(self):
        call("volume", {"level": 130})
        self.assertIn(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "1.00"], self.sys.calls)
        call("volume", {"change": -15})
        self.assertIn(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.25"], self.sys.calls)
        call("volume", {"mute": "toggle"})
        self.assertIn(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"], self.sys.calls)
        self.assertTrue(call("volume", {"mute": "maybe"})["isError"])
        self.assertTrue(call("volume", {"level": "fort"})["isError"])

    def test_bluetooth(self):
        rows = call("bluetooth", {})["structuredContent"]["devices"]
        self.assertEqual([(d["name"], d["connected"], d["audio"]) for d in rows],
                         [("WH-1000XM5", False, True), ("Mouse", True, False)])
        call("bluetooth_connect", {"device": "wh-1000"})
        self.assertIn(["bluetoothctl", "connect", "AA:BB:CC:DD:EE:FF"], self.sys.calls)
        call("bluetooth_connect", {"device": "mouse", "connect": False})
        self.assertIn(["bluetoothctl", "disconnect", "11:22:33:44:55:66"], self.sys.calls)
        self.assertTrue(call("bluetooth_connect", {"device": "airpods"})["isError"])

    def test_failure_is_a_sentence(self):
        def fail(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, "", "Connection refused")
        with mock.patch("subprocess.run", fail):
            r = call("devices", {})
        self.assertTrue(r["isError"])
        self.assertIn("Connection refused", r["content"][0]["text"])

    def test_protocol(self):
        tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["devices", "use", "volume", "bluetooth", "bluetooth_connect"])


if __name__ == "__main__":
    unittest.main()
