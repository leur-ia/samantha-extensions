"""python3 -m unittest discover contrib/media"""

import json
import subprocess
import unittest
from unittest import mock

import server

LIST = [{"name": ":1.4"}, {"name": "org.mpris.MediaPlayer2.firefox.instance_1_42"},
        {"name": "org.mpris.MediaPlayer2.spotify"}, {"name": "org.freedesktop.Notifications"}]


class Bus:
    def __init__(self, states, listing=LIST):
        self.states, self.listing, self.calls = states, listing, []

    def __call__(self, argv, capture_output, text, timeout):
        assert timeout
        args = argv[3:]
        self.calls.append(args)
        ok = lambda out: subprocess.CompletedProcess(argv, 0, json.dumps(out), "")
        if args[0] == "list":
            return ok(self.listing)
        if args[0] == "get-property":
            player, name = args[1], args[-1]
            short = player.split(".")[3]
            if name == "PlaybackStatus":
                return ok({"type": "s", "data": self.states[short]})
            if name == "Identity":
                return ok({"type": "s", "data": short.capitalize()})
            if name == "Metadata":
                return ok({"type": "a{sv}", "data": {
                    "xesam:title": {"type": "s", "data": f"{short} song"},
                    "xesam:artist": {"type": "as", "data": ["A", "B"]},
                    "mpris:length": {"type": "x", "data": 180000000}}})
        return subprocess.CompletedProcess(argv, 0, "", "")


def call(name, arguments):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    return r["result"]


class Media(unittest.TestCase):
    def test_now_playing_prefers_the_playing_one(self):
        with mock.patch("subprocess.run", Bus({"firefox": "Paused", "spotify": "Playing"})):
            r = call("now_playing", {})["structuredContent"]
        self.assertEqual(r, {"player": "spotify", "name": "Spotify", "state": "playing",
                             "title": "spotify song", "artists": "A, B", "album": "", "length_s": 180})

    def test_players_and_named_control(self):
        bus = Bus({"firefox": "Paused", "spotify": "Paused"})
        with mock.patch("subprocess.run", bus):
            self.assertEqual(len(call("players", {})["structuredContent"]["players"]), 2)
            r = call("control", {"action": "toggle", "player": "fire"})
        self.assertEqual(r["structuredContent"]["player"], "firefox.instance_1_42")
        self.assertEqual(bus.calls[-1][-1], "PlayPause")

    def test_errors(self):
        with mock.patch("subprocess.run", Bus({}, listing=[{"name": ":1.4"}])):
            self.assertIn("Aucun lecteur", call("now_playing", {})["content"][0]["text"])
        with mock.patch("subprocess.run", Bus({"firefox": "Paused", "spotify": "Paused"})):
            self.assertIn("spotify", call("control", {"action": "play", "player": "vlc"})["content"][0]["text"])
            self.assertTrue(call("control", {"action": "eject"})["isError"])
            self.assertTrue(call("volume", {"level": "fort"})["isError"])

    def test_volume_is_a_fraction(self):
        bus = Bus({"firefox": "Playing", "spotify": "Paused"})
        with mock.patch("subprocess.run", bus):
            call("volume", {"level": 150})
        self.assertEqual(bus.calls[-1][-3:], ["Volume", "d", "1.0"])

    def test_protocol(self):
        tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["players", "now_playing", "control", "volume"])
        self.assertEqual(server.handle({"jsonrpc": "2.0", "id": 2, "method": "x"})["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
