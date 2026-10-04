"""python3 -m unittest discover extensions/osm"""

import io
import json
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import server

ELEMENTS = [{"type": "node", "id": 1, "tags": {"name": "Nuvoletta", "amenity": "restaurant", "cuisine": "italian;pizza",
                                              "addr:housenumber": "10", "addr:street": "Rue Tonduti", "addr:city": "Nice",
                                              "phone": "+33 4 23", "opening_hours": "Tu-Su 12:00-14:30", "reservation": "yes"}},
            {"type": "way", "id": 2, "tags": {"amenity": "restaurant"}}]


class Osm:
    def __init__(self, busy_first=False):
        self.calls, self.busy_first = [], busy_first

    def __call__(self, req, timeout=None):
        assert timeout and req.get_header("User-agent").startswith("Samantha")
        self.calls.append((req.full_url, urllib.parse.unquote_plus(req.data.decode()) if req.data else ""))
        if "nominatim" in req.full_url:
            return io.BytesIO(json.dumps([{"lat": "43.69", "lon": "7.27"}]).encode())
        if self.busy_first and len([c for c in self.calls if "interpreter" in c[0]]) == 1:
            raise urllib.error.HTTPError(req.full_url, 504, "busy", {}, io.BytesIO(b""))
        return io.BytesIO(json.dumps({"elements": ELEMENTS}).encode())


def call(name, args):
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})["result"]


class OsmTest(unittest.TestCase):
    def test_search_maps_cuisine_and_falls_back(self):
        o = Osm(busy_first=True)
        with mock.patch("urllib.request.urlopen", o):
            r = call("search", {"near": "Vieux-Nice", "query": "italien"})["structuredContent"]["results"]
        self.assertEqual(r, [{"id": "node/1", "name": "Nuvoletta", "address": "10 Rue Tonduti, Nice", "cuisine": "italian, pizza",
                              "maps_url": "https://www.openstreetmap.org/node/1", "phone": "+33 4 23", "hours": "Tu-Su 12:00-14:30"}])
        queries = [c for c in o.calls if "interpreter" in c[0]]
        self.assertEqual(len(queries), 2, "the second instance answered")
        self.assertIn('["cuisine"~"italian",i]', queries[1][1])

    def test_names_are_escaped(self):
        self.assertEqual(server.filter_for('La "Merenda" (Nice)'), r'["name"~"la merenda \\(nice\\)",i]')

    def test_booking(self):
        with mock.patch("urllib.request.urlopen", Osm()):
            b = call("booking", {"id": "node/1"})["structuredContent"]
        self.assertEqual((b["reservable"], b["phone"]), (True, "+33 4 23"))
        self.assertTrue(call("booking", {"id": "relation/1"})["isError"])
        self.assertTrue(call("search", {"query": "x"})["isError"])

    def test_protocol(self):
        names = [t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
        self.assertEqual(names, ["search", "details", "booking"])


if __name__ == "__main__":
    unittest.main()
