"""python3 -m unittest discover contrib/spotify"""

import io
import json
import os
import subprocess
import unittest
import urllib.error
from unittest import mock

import server

META = {"type": "a{sv}", "data": {
    "mpris:trackid": {"type": "s", "data": "/com/spotify/track/4uLU6hMCjMI75M1A2tKUQC"},
    "mpris:length": {"type": "t", "data": 213000000},
    "xesam:title": {"type": "s", "data": "Never Gonna Give You Up"},
    "xesam:artist": {"type": "as", "data": ["Rick Astley"]},
    "xesam:album": {"type": "s", "data": "Whenever You Need Somebody"}}}


class Bus:
    """Stands in for subprocess.run: answers busctl calls, records them."""

    def __init__(self, running=True, state="Playing"):
        self.running, self.state, self.calls = running, state, []

    def __call__(self, argv, capture_output, text, timeout):
        assert timeout and argv[:3] == ["busctl", "--user", "--json=short"]
        self.calls.append(argv[3:])
        if not self.running:
            return subprocess.CompletedProcess(argv, 1, "", "Call failed: The name "
                                               "org.mpris.MediaPlayer2.spotify was not provided by any .service files")
        if argv[3] == "get-property":
            name = argv[-1]
            out = {"PlaybackStatus": {"type": "s", "data": self.state},
                   "Metadata": META, "Position": {"type": "x", "data": 61000000}}[name]
            return subprocess.CompletedProcess(argv, 0, json.dumps(out), "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def call(name, arguments):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    return r["result"]


class Player(unittest.TestCase):
    def test_now_playing(self):
        with mock.patch("subprocess.run", Bus()):
            r = call("now_playing", {})
        self.assertFalse(r["isError"])
        self.assertEqual(r["structuredContent"], {
            "running": True, "state": "playing", "title": "Never Gonna Give You Up",
            "artists": "Rick Astley", "album": "Whenever You Need Somebody",
            "uri": "spotify:track:4uLU6hMCjMI75M1A2tKUQC", "position_s": 61,
            "length_s": 213})

    def test_not_running(self):
        with mock.patch("subprocess.run", Bus(running=False)):
            self.assertFalse(call("now_playing", {})["structuredContent"]["running"])
            r = call("control", {"action": "next"})
        self.assertTrue(r["isError"])
        self.assertIn("pas lancé", r["content"][0]["text"])

    def test_control_and_open(self):
        bus = Bus()
        with mock.patch("subprocess.run", bus):
            self.assertFalse(call("control", {"action": "toggle"})["isError"])
            self.assertTrue(call("control", {"action": "stop"})["isError"])
            self.assertFalse(call("open", {"uri": "spotify:artist:0gxyHStUsqpMadRV0Di1Qt"})["isError"])
            self.assertTrue(call("open", {"uri": "https://open.spotify.com/x"})["isError"])
            self.assertTrue(call("open", {"uri": "spotify:track:x; rm -rf"})["isError"])
        self.assertEqual(bus.calls[0][-1], "PlayPause")
        self.assertEqual(bus.calls[1][-3:], ["OpenUri", "s", "spotify:artist:0gxyHStUsqpMadRV0Di1Qt"])
        self.assertEqual(len(bus.calls), 2, "invalid input never reaches the bus")

    def test_player_role_lists_only_player_tools(self):
        self.assertEqual(set(server.PLAYER_TOOLS), {"now_playing", "control", "open"})
        self.assertEqual(set(server.CATALOG_TOOLS), {"search"})


class Api:
    def __init__(self):
        self.requests = []

    def __call__(self, req, timeout=None):
        assert timeout
        self.requests.append(req)
        if req.full_url == server.TOKEN_URL:
            return io.BytesIO(b'{"access_token": "tok", "expires_in": 3600}')
        return io.BytesIO(json.dumps({"tracks": {"items": [
            {"name": "Song", "uri": "spotify:track:abc", "artists": [{"name": "A"}, {"name": "B"}],
             "album": {"name": "Al"}}, None]}}).encode())


class Catalog(unittest.TestCase):
    def setUp(self):
        server._token.update(value=None, expires=0.0)

    def test_search_caches_the_token(self):
        api = Api()
        env = {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "sec"}
        with mock.patch.dict(os.environ, env), mock.patch("urllib.request.urlopen", api):
            r = call("search", {"query": "song", "limit": 50})
            call("search", {"query": "other"})
        self.assertEqual(r["structuredContent"]["results"],
                         [{"name": "Song", "artists": "A, B", "uri": "spotify:track:abc", "album": "Al"}])
        urls = [q.full_url for q in api.requests]
        self.assertEqual(urls.count(server.TOKEN_URL), 1, "one token for both searches")
        self.assertIn("limit=10", urls[1], "capped at the API's 10")
        self.assertEqual(api.requests[1].get_header("Authorization"), "Bearer tok")

    def test_missing_keys(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            r = call("search", {"query": "x"})
        self.assertTrue(r["isError"])
        self.assertIn("set-key spotify-client-id", r["content"][0]["text"])

    def test_bad_keys_never_echoed(self):
        def refuse(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b"{}"))
        env = {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "s3cr3t"}
        with mock.patch.dict(os.environ, env), mock.patch("urllib.request.urlopen", refuse):
            r = call("search", {"query": "x"})
        self.assertTrue(r["isError"])
        self.assertNotIn("s3cr3t", r["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
