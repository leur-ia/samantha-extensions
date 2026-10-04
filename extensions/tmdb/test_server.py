"""python3 -m unittest discover extensions/tmdb"""

import io
import json
import os
import unittest
import urllib.parse
from unittest import mock

import server


class Tmdb:
    def __init__(self):
        self.requests = []

    def __call__(self, req, timeout=None):
        assert timeout and req.get_header("Authorization") == "Bearer t0k"
        url = urllib.parse.urlparse(req.full_url)
        path, q = url.path.removeprefix("/3"), dict(urllib.parse.parse_qsl(url.query))
        self.requests.append((path, q))
        if path == "/search/multi":
            body = {"results": [{"media_type": "person", "id": 1, "name": "X"},
                                {"media_type": "movie", "id": 693134, "title": "Dune : Deuxième partie", "release_date": "2024-02-27", "overview": "Paul…"}]}
        elif path == "/movie/693134":
            body = {"title": "Dune : Deuxième partie", "release_date": "2024-02-27", "vote_average": 8.15, "vote_count": 6000,
                    "runtime": 166, "genres": [{"name": "Science-Fiction"}], "overview": "Paul…",
                    "credits": {"crew": [{"job": "Director", "name": "Denis Villeneuve"}], "cast": [{"name": "Zendaya"}]}}
        elif path == "/movie/693134/watch/providers":
            body = {"results": {"FR": {"link": "https://www.themoviedb.org/movie/693134/watch?locale=FR",
                                       "flatrate": [{"provider_name": "Max"}], "rent": [{"provider_name": "Apple TV"}]}}}
        elif path.startswith("/trending/"):
            body = {"results": [{"media_type": "tv", "id": 1399, "name": "Game of Thrones", "first_air_date": "2011-04-17"}]}
        else:
            body = {}
        return io.BytesIO(json.dumps(body).encode())


def call(name, args):
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})["result"]


class TmdbTest(unittest.TestCase):
    def setUp(self):
        self.t = Tmdb()
        for p in (mock.patch.dict(os.environ, {"TMDB_TOKEN": "t0k"}), mock.patch("urllib.request.urlopen", self.t),
                  mock.patch.object(server, "LANGUAGE", "fr-FR"), mock.patch.object(server, "REGION", "FR")):
            p.start()
            self.addCleanup(p.stop)

    def test_search_skips_people(self):
        r = call("search", {"query": "dune"})["structuredContent"]["results"]
        self.assertEqual([(x["id"], x["year"], x["kind"]) for x in r], [("movie/693134", 2024, "movie")])
        self.assertEqual(self.t.requests[0][1]["language"], "fr-FR")

    def test_ratings_details_watch_trending(self):
        self.assertEqual(call("ratings", {"title": "dune 2"})["structuredContent"]["ratings"][0]["value"], "8.2/10 (6000 votes)")
        d = call("details", {"id": "movie/693134"})["structuredContent"]
        self.assertEqual((d["director"], d["runtime"], d["cast"]), ("Denis Villeneuve", "166 min", "Zendaya"))
        w = call("watch", {"id": "movie/693134"})["structuredContent"]
        self.assertEqual((w["stream"], w["rent"], w["credit"]), (["Max"], ["Apple TV"], "JustWatch"))
        self.assertEqual(call("trending", {"kind": "series"})["structuredContent"]["results"][0]["id"], "tv/1399")
        self.assertEqual(self.t.requests[-1][0], "/trending/tv/week")
        self.assertTrue(call("details", {"id": "person/1"})["isError"])

    def test_missing_token(self):
        with mock.patch.dict(os.environ, {"TMDB_TOKEN": ""}):
            self.assertIn("set-key tmdb", call("search", {"query": "x"})["content"][0]["text"])

    def test_protocol(self):
        names = [t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
        self.assertEqual(names, ["search", "ratings", "details", "trending", "watch"])


if __name__ == "__main__":
    unittest.main()
