"""python3 -m unittest discover contrib/home-assistant"""

import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

import server

STATES = {
    "light.salon": {"entity_id": "light.salon", "state": "off", "attributes": {"friendly_name": "Salon", "brightness": None}},
    "sensor.temp_salon": {"entity_id": "sensor.temp_salon", "state": "21.5",
                          "attributes": {"friendly_name": "Température salon", "unit_of_measurement": "°C"}},
    "cover.garage": {"entity_id": "cover.garage", "state": "closed", "attributes": {"friendly_name": "Garage", "device_class": "garage"}},
    "cover.store": {"entity_id": "cover.store", "state": "open", "attributes": {"friendly_name": "Store", "device_class": "shade"}},
    "lock.entree": {"entity_id": "lock.entree", "state": "locked", "attributes": {"friendly_name": "Entrée"}},
}


class HA:
    def __init__(self):
        self.calls = []

    def __call__(self, req, timeout=None):
        assert timeout and req.get_header("Authorization") == "Bearer tok"
        path = req.full_url.removeprefix("http://ha.local:8123")
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.get_method(), path, body))
        if path == "/api/states":
            out = list(STATES.values())
        elif path.startswith("/api/states/"):
            eid = path.rsplit("/", 1)[1]
            if eid not in STATES:
                raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b""))
            out = STATES[eid]
        elif path == "/api/template":
            out = '["light.salon", "sensor.temp_salon"]'
            return io.BytesIO(out.encode())
        else:
            out = [STATES["lock.entree"]]
        return io.BytesIO(json.dumps(out).encode())


def call(name, arguments):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    return r["result"]


class HomeAssistant(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        os.makedirs(os.path.join(d.name, "samantha"))
        with open(os.path.join(d.name, "samantha", "home-assistant.toml"), "w") as f:
            f.write('url = "http://ha.local:8123/"\n')
        self.ha = HA()
        for p in (mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d.name, "HA_TOKEN": "tok"}),
                  mock.patch("urllib.request.urlopen", self.ha), mock.patch("time.sleep")):
            p.start()
            self.addCleanup(p.stop)

    def test_entities_filters(self):
        r = call("entities", {"domain": "sensor"})["structuredContent"]
        self.assertEqual(r["entities"], [{"entity_id": "sensor.temp_salon", "name": "Température salon",
                                          "state": "21.5", "unit_of_measurement": "°C"}])
        self.assertEqual(call("entities", {"area": "Salon"})["structuredContent"]["count"], 2)
        self.assertEqual(call("entities", {"search": "garage"})["structuredContent"]["count"], 1)

    def test_control(self):
        call("control", {"entity_id": "light.salon", "action": "set", "brightness": 140})
        self.assertIn(("POST", "/api/services/light/turn_on", {"entity_id": "light.salon", "brightness_pct": 100}), self.ha.calls)
        call("control", {"entity_id": "cover.store", "action": "close"})
        self.assertIn(("POST", "/api/services/cover/close_cover", {"entity_id": "cover.store"}), self.ha.calls)

    def test_guarded_things_need_call_service(self):
        for args in ({"entity_id": "cover.garage", "action": "open"}, {"entity_id": "lock.entree", "action": "off"}):
            r = call("control", args)
            self.assertTrue(r["isError"])
            self.assertIn("call_service", r["content"][0]["text"])
        self.assertFalse(any(m == "POST" and "/services/" in p for m, p, _ in self.ha.calls))
        r = call("call_service", {"domain": "lock", "service": "unlock", "data": {"entity_id": "lock.entree"}})
        self.assertEqual(r["structuredContent"]["done"], "lock.unlock")
        self.assertTrue(call("call_service", {"domain": "../x", "service": "y"})["isError"])

    def test_bad_input_and_errors(self):
        self.assertTrue(call("state", {"entity_id": "x"})["isError"])
        self.assertIn("Introuvable", call("state", {"entity_id": "light.cave"})["content"][0]["text"])
        self.assertTrue(call("control", {"entity_id": "light.salon", "action": "set"})["isError"])
        with mock.patch.dict(os.environ, {"HA_TOKEN": ""}):
            self.assertIn("set-key home-assistant", call("entities", {})["content"][0]["text"])

    def test_protocol(self):
        tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["entities", "state", "control", "call_service"])


if __name__ == "__main__":
    unittest.main()
