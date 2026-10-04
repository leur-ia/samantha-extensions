"""python3 -m unittest discover extensions/omdb"""

import io
import json
import os
import unittest
import urllib.parse
from unittest import mock

import server

DUNE = {"Response": "True", "Title": "Dune: Part Two", "Year": "2024", "Type": "movie", "imdbID": "tt15239678",
        "Plot": "Paul Atreides unites with the Fremen.", "Genre": "Action, Adventure", "Runtime": "166 min",
        "Director": "Denis Villeneuve", "Actors": "Timothée Chalamet, Zendaya", "imdbRating": "8.5",
        "Ratings": [{"Source": "Internet Movie Database", "Value": "8.5/10"},
                    {"Source": "Rotten Tomatoes", "Value": "92%"}, {"Source": "Metacritic", "Value": "79/100"}]}


class Omdb:
    def __init__(self):
        self.queries = []

    def __call__(self, url, timeout=None):
        assert timeout
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        self.queries.append(q)
        if "s" in q:
            body = {"Response": "True", "Search": [{"Title": DUNE["Title"], "Year": "2024", "imdbID": "tt15239678", "Type": "movie"},
                                                   {"Title": "Bad", "imdbID": "x"}]}
        elif q.get("t") == "nope":
            body = {"Response": "False", "Error": "Movie not found!"}
        else:
            body = DUNE
        return io.BytesIO(json.dumps(body).encode())


def call(name, args):
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})["result"]


class OmdbTest(unittest.TestCase):
    def setUp(self):
        self.o = Omdb()
        for p in (mock.patch.dict(os.environ, {"OMDB_KEY": "k3y"}), mock.patch("urllib.request.urlopen", self.o)):
            p.start()
            self.addCleanup(p.stop)

    def test_ratings_name_sources(self):
        r = call("ratings", {"title": "Dune 2", "year": 2024})["structuredContent"]["ratings"]
        self.assertEqual([(x["source"], x["value"]) for x in r], [("IMDb", "8.5/10"), ("Rotten Tomatoes", "92%"), ("Metacritic", "79/100")])
        self.assertEqual((self.o.queries[0]["t"], self.o.queries[0]["y"], self.o.queries[0]["apikey"]), ("Dune 2", "2024", "k3y"))
        self.assertEqual(call("ratings", {"title": "nope"})["structuredContent"]["ratings"], [])

    def test_search_and_details(self):
        r = call("search", {"query": "dune", "kind": "movie"})["structuredContent"]["results"]
        self.assertEqual(r, [{"id": "tt15239678", "title": "Dune: Part Two", "year": 2024, "kind": "movie"}])
        d = call("details", {"id": "tt15239678"})["structuredContent"]
        self.assertEqual((d["director"], d["runtime"]), ("Denis Villeneuve", "166 min"))
        self.assertTrue(call("details", {"id": "15239678"})["isError"])
        self.assertTrue(call("search", {"query": "dune", "kind": "anime"})["isError"])

    def test_missing_key_never_echoed(self):
        with mock.patch.dict(os.environ, {"OMDB_KEY": ""}):
            self.assertIn("set-key omdb", call("ratings", {"title": "x"})["content"][0]["text"])

    def test_protocol(self):
        names = [t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
        self.assertEqual(names, ["search", "ratings", "details"])


if __name__ == "__main__":
    unittest.main()
