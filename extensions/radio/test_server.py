"""python3 -m unittest discover contrib/radio"""

import io
import json
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import server

INTER = {"stationuuid": "33960c43", "name": " France Inter ", "countrycode": "FR",
         "tags": "news,talk", "codec": "AAC", "bitrate": 192,
         "url_resolved": "https://stream.radiofrance.fr/franceinter.m3u8"}


class Directory:
    def __init__(self, fail_first=False):
        self.urls, self.fail_first = [], fail_first

    def __call__(self, req, timeout=None):
        assert timeout and req.get_header("User-agent").startswith("Samantha")
        self.urls.append(req.full_url)
        if self.fail_first and len(self.urls) == 1:
            raise urllib.error.URLError("down")
        path = urllib.parse.urlparse(req.full_url).path
        body = [INTER] if path.startswith(("/json/stations", )) else {"ok": True}
        return io.BytesIO(json.dumps(body).encode())


class Proc:
    def __init__(self, argv, **kw):
        self.argv, self.done = argv, False
        Proc.started.append(self)

    def poll(self):
        return 0 if self.done else None

    def wait(self, timeout=None):
        while not self.done:
            import time
            time.sleep(0.01)

    def terminate(self):
        self.done = True

    kill = terminate


def call(name, arguments):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    return r["result"]


class Radio(unittest.TestCase):
    def setUp(self):
        Proc.started = []
        self.island = []
        for p in (mock.patch("subprocess.Popen", Proc),
                  mock.patch.object(server, "daemon_publish", self.island.append)):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        server.stop({})

    def test_search_shapes_and_falls_back_to_a_mirror(self):
        d = Directory(fail_first=True)
        with mock.patch("urllib.request.urlopen", d):
            r = call("search", {"query": "france inter", "country": "fr"})["structuredContent"]
        self.assertEqual(r["stations"][0]["name"], "France Inter")
        self.assertIn("countrycode=FR", d.urls[1])
        self.assertTrue(d.urls[1].startswith(server.MIRRORS[1]))
        self.assertTrue(call("search", {})["isError"])

    def test_play_replaces_and_stop_clears(self):
        with mock.patch("urllib.request.urlopen", Directory()):
            r = call("play", {"query": "france inter"})["structuredContent"]
            call("play", {"id": "33960c43"})
        self.assertEqual(r["playing"], "France Inter")
        self.assertEqual(Proc.started[0].argv[-1], INTER["url_resolved"])
        self.assertTrue(Proc.started[0].done, "the first stream stopped for the second")
        self.assertEqual(self.island[-1], {"key": "radio", "text": "Radio · France Inter", "sub": "FR"})
        self.assertTrue(call("now", {})["structuredContent"]["playing"])
        self.assertEqual(call("stop", {})["structuredContent"]["stopped"], "France Inter")
        self.assertEqual(self.island[-1], {"key": "radio"})
        self.assertFalse(call("now", {})["structuredContent"]["playing"])

    def test_errors(self):
        self.assertTrue(call("play", {})["isError"])
        with mock.patch("urllib.request.urlopen", lambda req, timeout=None: (_ for _ in ()).throw(urllib.error.URLError("x"))):
            self.assertIn("injoignable", call("search", {"tag": "jazz"})["content"][0]["text"])

    def test_protocol(self):
        tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["search", "play", "stop", "now"])


if __name__ == "__main__":
    unittest.main()
