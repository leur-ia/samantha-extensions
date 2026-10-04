"""python3 -m unittest discover extensions/duffel"""

import io
import json
import os
import unittest
import urllib.parse
from unittest import mock

import server

def seg(fr, to, dep, arr, num):
    return {"origin": {"iata_code": fr}, "destination": {"iata_code": to}, "departing_at": dep, "arriving_at": arr,
            "marketing_carrier": {"iata_code": "AF"}, "marketing_carrier_flight_number": num,
            "aircraft": {"name": "Airbus A320"}, "passengers": [{"baggages": [{"type": "checked", "quantity": 1}]}]}

OFFERS = [
    {"id": "off_2", "total_amount": "212.40", "total_currency": "EUR", "owner": {"name": "Air France"},
     "slices": [{"duration": "PT3H10M", "segments": [seg("NCE", "LYS", "2026-10-09T07:00:00", "2026-10-09T08:05:00", "7701"),
                                                     seg("LYS", "CDG", "2026-10-09T09:00:00", "2026-10-09T10:10:00", "7643")]}]},
    {"id": "off_1", "total_amount": "89.00", "total_currency": "EUR", "owner": {"name": "easyJet"},
     "slices": [{"duration": "PT1H30M", "segments": [seg("NCE", "ORY", "2026-10-09T18:00:00", "2026-10-09T19:30:00", "4012")]}],
     "conditions": {"change_before_departure": {"allowed": True, "penalty_amount": "50.00", "penalty_currency": "EUR"},
                    "refund_before_departure": {"allowed": False}}},
]


class Duffel:
    def __init__(self):
        self.requests = []

    def __call__(self, req, timeout=None):
        assert timeout and req.get_header("Authorization") == "Bearer duffel_test_x" and req.get_header("Duffel-version") == "v2"
        u = urllib.parse.urlparse(req.full_url)
        body = json.loads(req.data) if req.data else None
        self.requests.append((req.get_method(), u.path, dict(urllib.parse.parse_qsl(u.query)), body))
        if u.path == "/places/suggestions":
            data = [{"type": "airport", "iata_code": "NCE", "name": "Nice Côte d'Azur", "city_name": "Nice"},
                    {"type": "city", "iata_city_code": "NCE", "name": "Nice"}]
        elif u.path == "/air/offer_requests":
            data = {"offers": OFFERS}
        else:
            data = OFFERS[1]
        r = io.BytesIO(json.dumps({"data": data}).encode())
        r.headers = {}
        return r


def call(name, args):
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})["result"]


class DuffelTest(unittest.TestCase):
    def setUp(self):
        self.d = Duffel()
        for p in (mock.patch.dict(os.environ, {"DUFFEL_TOKEN": "duffel_test_x"}), mock.patch("urllib.request.urlopen", self.d)):
            p.start()
            self.addCleanup(p.stop)

    def test_places(self):
        self.assertEqual(call("places", {"query": "nice"})["structuredContent"]["places"][0], {"code": "NCE", "name": "Nice Côte d'Azur", "city": "Nice", "kind": "airport"})

    def test_search_cheapest_first(self):
        r = call("search", {"from": "nce", "to": "PAR", "date": "2026-10-09", "adults": 2, "max_stops": 1})["structuredContent"]["offers"]
        self.assertEqual([(o["id"], o["price"], o["airline"], o["stops"], o["duration"]) for o in r],
                         [("off_1", 89.0, "easyJet", 0, "1 h 30"), ("off_2", 212.4, "Air France", 1, "3 h 10")])
        self.assertEqual(r[1]["outbound"]["flights"], ["AF7701", "AF7643"])
        body = self.d.requests[0][3]["data"]
        self.assertEqual((body["slices"][0]["origin"], len(body["passengers"]), body["max_connections"]), ("NCE", 2, 1))
        for bad in ({"from": "Nice", "to": "CDG", "date": "2026-10-09"}, {"from": "NCE", "to": "CDG", "date": "vendredi"},
                    {"from": "NCE", "to": "CDG", "date": "2026-10-09", "cabin": "luxe"}):
            self.assertTrue(call("search", bad)["isError"])

    def test_details_and_book_url(self):
        d = call("details", {"id": "off_1"})["structuredContent"]
        self.assertEqual((d["changes"], d["refund"]), ("oui (50.00 EUR)", "non"))
        self.assertEqual(d["segments"][0]["bags"], "1 checked")
        self.assertIn("Flights+from+NCE+to+ORY+on+2026-10-09", d["book_url"])
        self.assertTrue(call("details", {"id": "../x"})["isError"])

    def test_missing_token(self):
        with mock.patch.dict(os.environ, {"DUFFEL_TOKEN": ""}):
            self.assertIn("set-key duffel", call("places", {"query": "x"})["content"][0]["text"])

    def test_protocol(self):
        names = [t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]]
        self.assertEqual(names, ["places", "search", "details"])


if __name__ == "__main__":
    unittest.main()
