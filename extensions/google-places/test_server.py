"""python3 -m unittest discover extensions/google-places"""

import io
import json
import os
import unittest
from unittest import mock

import server

PLACE = {"id": "ChIJabcdefghij", "displayName": {"text": "La Merenda"}, "formattedAddress": "4 Rue Raoul Bosio, Nice",
         "rating": 4.6, "userRatingCount": 1200, "priceLevel": "PRICE_LEVEL_MODERATE", "currentOpeningHours": {"openNow": True},
         "primaryTypeDisplayName": {"text": "Restaurant niçois"}, "googleMapsUri": "https://maps.google.com/?cid=1",
         "nationalPhoneNumber": "04 93 00 00 00", "reservable": False,
         "regularOpeningHours": {"weekdayDescriptions": ["lundi: Fermé"]}, "reviews": [{"rating": 5, "text": {"text": "Excellent"}}]}


class Places:
    def __init__(self):
        self.requests = []

    def __call__(self, req, timeout=None):
        assert timeout and req.get_header("X-goog-api-key") == "k3y"
        self.requests.append((req.get_method(), req.full_url, req.get_header("X-goog-fieldmask"), json.loads(req.data) if req.data else None))
        body = {"places": [PLACE]} if req.full_url.endswith(":searchText") else PLACE
        return io.BytesIO(json.dumps(body).encode())


def call(name, args):
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})["result"]


class PlacesTest(unittest.TestCase):
    def setUp(self):
        self.p = Places()
        for x in (mock.patch.dict(os.environ, {"GOOGLE_PLACES_KEY": "k3y"}), mock.patch("urllib.request.urlopen", self.p)):
            x.start()
            self.addCleanup(x.stop)

    def test_search(self):
        r = call("search", {"near": "Nice", "query": "niçois", "open_now": True, "limit": 50})["structuredContent"]["results"][0]
        self.assertEqual((r["name"], r["rating"], r["reviews"], r["price"], r["open_now"]), ("La Merenda", 4.6, 1200, "€€", True))
        method, url, mask, body = self.p.requests[0]
        self.assertEqual((body["textQuery"], body["openNow"], body["pageSize"]), ("niçois restaurant Nice", True, 20))
        self.assertIn("places.rating", mask)
        self.assertTrue(call("search", {"query": "x"})["isError"])

    def test_details_and_booking(self):
        d = call("details", {"id": "ChIJabcdefghij"})["structuredContent"]
        self.assertEqual((d["hours"], d["reviews_text"][0]["text"], d["reservable"]), (["lundi: Fermé"], "Excellent", False))
        b = call("booking", {"id": "ChIJabcdefghij"})["structuredContent"]
        self.assertEqual((b["phone"], b["reservable"]), ("04 93 00 00 00", False))
        self.assertTrue(call("details", {"id": "../x"})["isError"])

    def test_missing_key(self):
        with mock.patch.dict(os.environ, {"GOOGLE_PLACES_KEY": ""}):
            self.assertIn("set-key google-places", call("search", {"near": "Nice"})["content"][0]["text"])

    def test_protocol(self):
        names = [t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
        self.assertEqual(names, ["search", "details", "booking"])


if __name__ == "__main__":
    unittest.main()
