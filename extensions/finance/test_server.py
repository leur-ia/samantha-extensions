"""python3 -m unittest discover -s . -p 'test_*.py' (no network: urlopen and openssl are stubbed)"""

import base64
import datetime as dt
import io
import json
import os
import tempfile
import tomllib
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import server

HERE = os.path.dirname(os.path.abspath(__file__))
UID = "11111111-2222-3333-4444-555555555555"


def tx(date, amount, remittance=None, creditor=None, debtor=None, ref=None):
    t = {"transaction_amount": {"amount": f"{abs(amount):.2f}", "currency": "EUR"},
         "credit_debit_indicator": "DBIT" if amount < 0 else "CRDT", "booking_date": date,
         "status": "BOOK"}
    if remittance:
        t["remittance_information"] = remittance
    if creditor:
        t["creditor"] = {"name": creditor}
    if debtor:
        t["debtor"] = {"name": debtor}
    if ref:
        t["entry_reference"] = ref
    return t


PAGE1 = {"transactions": [tx("2026-10-03", -42.5, ["CB CARREFOUR", "MARKET NICE"], ref="r1"),
                          tx("2026-10-01", 2500, debtor="ACME SAS", remittance=["SALAIRE 09/2026"], ref="r2")],
         "continuation_key": "k2"}
PAGE2 = {"transactions": [tx("2026-09-28", -12.99, creditor="Netflix")]}
BALANCES = {"balances": [{"name": "x", "balance_amount": {"amount": "1234.56", "currency": "EUR"},
                          "balance_type": "CLAV"}]}
ASPSPS = {"aspsps": [{"name": "Fortuneo", "country": "FR", "maximum_consent_validity": 15552000},
                     {"name": "BNP Paribas", "country": "FR", "maximum_consent_validity": 7776000}]}
SESSION = {"session_id": "sess-1", "aspsp": {"name": "Fortuneo", "country": "FR"},
           "access": {"valid_until": "2027-04-01T00:00:00+00:00"}, "psu_type": "personal",
           "accounts": [{"uid": UID, "name": "Compte courant", "product": "Compte", "currency": "EUR",
                         "cash_account_type": "CACC", "account_id": {"iban": "FR7612345"}}]}


class Api:
    """Stands in for urlopen: answers by path, records the requests."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, req, timeout=None):
        assert timeout, "every request has a timeout"
        url = req.full_url if hasattr(req, "full_url") else req
        parsed = urllib.parse.urlsplit(url)
        body = json.loads(req.data) if getattr(req, "data", None) else None
        self.calls.append((getattr(req, "method", "GET"), parsed.path, dict(urllib.parse.parse_qsl(parsed.query)),
                           body, getattr(req, "headers", {})))
        for key, answer in self.routes.items():
            if key in url:
                if isinstance(answer, Exception):
                    raise answer
                if callable(answer):
                    answer = answer(self.calls[-1])
                r = io.BytesIO(json.dumps(answer).encode())
                r.status = 200
                return r
        raise AssertionError(f"unexpected request {url}")


def http_error(code, error):
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(
        json.dumps({"message": "m", "code": code, "error": error}).encode()))


def call(name, arguments=None):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments or {}}})
    return r["result"]


def ok(name, arguments=None):
    r = call(name, arguments)
    assert not r["isError"], r["content"][0]["text"]
    return r["structuredContent"]


class Finance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = os.path.join(self.tmp.name, "config", "otto")
        os.makedirs(cfg)
        self.key = os.path.join(cfg, "finance.pem")
        with open(self.key, "w") as f:
            f.write("fake key")
        with open(os.path.join(cfg, "finance.toml"), "w") as f:
            f.write(f'app_id = "app-123"\nkey = "{self.key}"\n'
                    'redirect_url = "https://otto.example/connected.html"\n'
                    'household = ["Jean-Baptiste", "Marie"]\n'
                    f'[accounts."{UID}"]\nowner = "Jean-Baptiste"\n')
        env = {"XDG_CONFIG_HOME": os.path.join(self.tmp.name, "config"),
               "XDG_STATE_HOME": os.path.join(self.tmp.name, "state")}
        os.environ.pop("OTTO_SOCKET", None)
        for p in (mock.patch.dict(os.environ, env), mock.patch.dict(server._jwt, {"token": None, "exp": 0}),
                  mock.patch.object(server.subprocess, "run", self.openssl)):
            p.start()
            self.addCleanup(p.stop)
        self.signed = []

    def openssl(self, cmd, input=None, **kw):
        self.signed.append((cmd, input))
        return mock.Mock(returncode=0, stdout=b"\x01\x02sig")

    def serve(self, routes):
        api = Api(routes)
        p = mock.patch.object(server.urllib.request, "urlopen", api)
        p.start()
        self.addCleanup(p.stop)
        return api

    def seed(self, rows):
        server.append_transactions(rows)

    @staticmethod
    def row(i, date, amount, label, counterparty="", **extra):
        return {"id": f"t{i}", "account": UID, "date": date, "amount": amount, "currency": "EUR",
                "label": label, "counterparty": counterparty, "category": None, "shared": False,
                "paid_by": None, **extra}

    # ----- Enable Banking client

    def test_jwt_shape(self):
        token = server.jwt()
        cmd, signing_input = self.signed[0]
        self.assertEqual(cmd, ["openssl", "dgst", "-sha256", "-sign", self.key])
        h, p, sig = token.split(".")
        self.assertEqual(signing_input, f"{h}.{p}".encode())
        pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
        self.assertEqual(json.loads(pad(h)), {"typ": "JWT", "alg": "RS256", "kid": "app-123"})
        payload = json.loads(pad(p))
        self.assertEqual((payload["iss"], payload["aud"]), ("enablebanking.com", "api.enablebanking.com"))
        self.assertEqual(payload["exp"] - payload["iat"], 3600)
        self.assertEqual(pad(sig), b"\x01\x02sig")
        self.assertNotIn("=", token)
        self.assertEqual(server.jwt(), token)  # cached
        self.assertEqual(len(self.signed), 1)

    def test_connect_builds_auth_body(self):
        api = self.serve({"/aspsps": ASPSPS, "/auth": {"url": "https://auth.enablebanking.com/x",
                                                       "authorization_id": "a", "psu_id_hash": "h"}})
        r = ok("connect", {"bank": "fortuneo"})
        self.assertEqual(r["url"], "https://auth.enablebanking.com/x")
        self.assertIn("téléphone", r["next"])
        method, path, _, body, headers = api.calls[-1]
        self.assertEqual((method, path), ("POST", "/auth"))
        self.assertTrue(headers["Authorization"].startswith("Bearer "))
        self.assertEqual(body["aspsp"], {"name": "Fortuneo", "country": "FR"})
        self.assertEqual(body["redirect_url"], "https://otto.example/connected.html")
        self.assertEqual(body["psu_type"], "personal")
        self.assertEqual(body["access"]["balances"], True)
        self.assertEqual(len(body["state"]), 32)
        until = dt.datetime.fromisoformat(body["access"]["valid_until"])
        self.assertIsNotNone(until.tzinfo)
        self.assertLessEqual(until - server.now_utc(), dt.timedelta(days=180))
        self.assertGreater(until - server.now_utc(), dt.timedelta(days=179))
        self.assertIn(body["state"], server.sessions_list()["pending"])
        # BNP allows 90 days only.
        ok("connect", {"bank": "BNP"})
        until = dt.datetime.fromisoformat(api.calls[-1][3]["access"]["valid_until"])
        self.assertLessEqual(until - server.now_utc(), dt.timedelta(days=90))
        self.assertEqual(sum(1 for c in api.calls if c[1] == "/aspsps"), 1)  # cached
        self.assertTrue(call("connect", {"bank": "Banque Imaginaire"})["isError"])

    def test_connected_parses_url_and_stores_session(self):
        api = self.serve({"/sessions": SESSION})
        r = ok("connected", {"url": "https://otto.example/connected.html?code=abc123&state=st"})
        self.assertEqual(api.calls[-1][3], {"code": "abc123"})
        self.assertEqual(r["session_id"], "sess-1")
        self.assertEqual(r["bank"], "Fortuneo")
        self.assertEqual(r["accounts"][0]["iban"], "FR7612345")
        stored = server.sessions_list()["sessions"]
        self.assertEqual(stored[0]["accounts"][0]["uid"], UID)
        self.assertEqual(stored[0]["status"], "active")
        self.assertTrue(call("connected", {"url": "https://otto.example/connected.html"})["isError"])
        a = ok("accounts")["accounts"][0]
        self.assertEqual((a["bank"], a["balance"]), ("Fortuneo", None))

    def test_sync_normalises_pages_and_respects_windows(self):
        self.serve({"/sessions": SESSION})
        ok("connected", {"code": "c"})
        pages = {"k2": PAGE2}
        api = self.serve({"/transactions": lambda c: pages.get(c[2].get("continuation_key"), PAGE1),
                          "/balances": BALANCES})
        r = ok("sync")
        self.assertEqual((r["new"], r["accounts_synced"]), (3, 1))
        tcalls = [c for c in api.calls if c[1].endswith("/transactions")]
        self.assertEqual(len(tcalls), 2)
        self.assertIn("date_from", tcalls[0][2])
        self.assertEqual(tcalls[1][2], {"continuation_key": "k2"})
        rows = {t["id"]: t for t in ok("transactions")["transactions"]}
        self.assertEqual(rows["r1"]["amount"], -42.5)
        self.assertEqual(rows["r1"]["label"], "CB CARREFOUR MARKET NICE")
        self.assertEqual(rows["r1"]["category"], "courses")
        self.assertEqual((rows["r2"]["amount"], rows["r2"]["counterparty"], rows["r2"]["category"]),
                         (2500.0, "ACME SAS", "salaire"))
        netflix = next(t for t in rows.values() if t["counterparty"] == "Netflix")
        self.assertEqual((netflix["amount"], netflix["label"], netflix["category"]),
                         (-12.99, "Netflix", "abonnements"))
        self.assertEqual(ok("accounts")["accounts"][0]["balance"], 1234.56)
        # Second run within 6 h: skipped, no call; forced: nothing new (dedupe).
        n = len(api.calls)
        self.assertEqual(ok("sync")["accounts_skipped"], 1)
        self.assertEqual(len(api.calls), n)
        r = ok("sync", {"force": True})
        self.assertEqual((r["new"], r["accounts_synced"]), (0, 1))
        self.assertEqual(ok("transactions")["count"], 3)
        self.assertEqual(ok("transactions", {"category": "courses"})["count"], 1)
        self.assertEqual(ok("transactions", {"query": "netflix"})["count"], 1)

    def test_sync_rate_limit_and_expired_session(self):
        self.serve({"/sessions": SESSION})
        ok("connected", {"code": "c"})
        api = self.serve({"/transactions": http_error(429, None)})
        r = ok("sync")
        self.assertEqual(r["accounts_synced"], 0)
        self.assertIn("limite", r["notes"][0])
        n = len(api.calls)
        self.assertEqual(ok("sync", {"force": True})["accounts_skipped"], 1)  # backed off
        self.assertEqual(len(api.calls), n)
        server.write_json("sync.json", {})
        self.serve({"/transactions": http_error(401, "EXPIRED_SESSION")})
        r = ok("sync")
        self.assertIn("reconnecte", r["notes"][0])
        self.assertEqual(server.sessions_list()["sessions"][0]["status"], "expired")
        self.assertEqual(ok("accounts")["accounts"][0]["status"], "expired")

    def test_sync_publishes_events(self):
        self.serve({"/sessions": SESSION})
        ok("connected", {"code": "c"})
        self.serve({"/transactions": PAGE2, "/balances": BALANCES})
        events = []
        with mock.patch.dict(os.environ, {"OTTO_SOCKET": "/tmp/x"}), \
                mock.patch.object(server, "daemon", lambda m, p: events.append((m, p))):
            ok("sync")
        self.assertEqual(events[0][0], "events.publish")
        self.assertEqual(events[0][1]["type"], "finance.transaction")
        self.assertEqual(set(events[0][1]["data"]), {"id", "account", "date", "amount", "label", "category"})
        self.assertEqual(events[-1][1], {"type": "finance.synced", "data": {"new": 1, "accounts": 1}})

    def test_network_down_and_not_configured(self):
        self.serve({"/aspsps": urllib.error.URLError("no route")})
        self.assertIn("injoignable", call("banks")["content"][0]["text"])
        os.remove(os.path.join(os.environ["XDG_CONFIG_HOME"], "otto", "finance.toml"))
        server._jwt["token"] = None
        self.assertIn("finance.toml", call("banks")["content"][0]["text"])
        self.assertIn("setup.md", call("sync")["content"][0]["text"])
        self.assertIn("finance.connect", call("accounts")["content"][0]["text"])

    # ----- categorisation, budgets, recurring, shared

    def test_categorisation_defaults_and_user_rules(self):
        self.seed([self.row(1, "2026-10-02", -30, "CB LECLERC ST LAURENT"),
                   self.row(2, "2026-10-03", -15, "PRLV SEPA ORANGE SA"),
                   self.row(3, "2026-10-04", -9, "CB MYSTERE")])
        cats = {t["id"]: t["category"] for t in ok("transactions")["transactions"]}
        self.assertEqual(cats, {"t1": "courses", "t2": "logement", "t3": "autre"})
        ok("categorize", {"pattern": "leclerc", "category": "bricolage"})
        ok("categorize", {"transaction_id": "t3", "category": "cadeaux"})
        cats = {t["id"]: t["category"] for t in ok("transactions")["transactions"]}
        self.assertEqual(cats, {"t1": "bricolage", "t2": "logement", "t3": "cadeaux"})
        self.assertTrue(call("categorize", {"pattern": "x"})["isError"])
        self.assertTrue(call("categorize", {"transaction_id": "nope", "category": "x"})["isError"])

    def test_summary_against_budgets(self):
        self.seed([self.row(1, "2026-10-02", -120, "CARREFOUR"), self.row(2, "2026-10-05", -80, "LECLERC"),
                   self.row(3, "2026-10-06", -45, "RESTAURANT CHEZ LUI"), self.row(4, "2026-10-01", 2000, "SALAIRE"),
                   self.row(5, "2026-09-10", -100, "CARREFOUR")])
        ok("budget_set", {"category": "courses", "amount": 250})
        s = ok("summary", {"month": "2026-10"})
        self.assertEqual((s["income"], s["expenses"]), (2000, 245))
        courses = s["by_category"][0]
        self.assertEqual(courses, {"category": "courses", "spent": 200, "budget": 250, "ratio": 0.8})
        self.assertIsNone(s["by_category"][1]["budget"])
        self.assertEqual(s["biggest"][0]["amount"], -120)
        self.assertEqual(s["vs_previous"], {"month": "2026-09", "expenses": 100, "change_pct": 145})
        self.assertTrue(call("summary", {"month": "octobre"})["isError"])
        self.assertEqual(ok("budget_set", {"category": "courses", "amount": 0})["budgets"], {})

    def test_recurring_detection(self):
        rows = [self.row(i, f"2026-{m:02d}-05", -12.99, "Netflix", "Netflix") for i, m in enumerate((6, 7, 8, 9))]
        rows += [self.row(10 + i, f"2026-{m:02d}-20", -40 - i * 5, "CB CARREFOUR 123", "") for i, m in enumerate((6, 7, 8, 9))]
        rows += [self.row(20, "2026-07-01", -9, "SPOTIFY", "Spotify"), self.row(21, "2026-09-01", -9, "SPOTIFY", "Spotify")]
        self.seed(rows)
        r = ok("recurring")
        self.assertEqual([x["name"] for x in r["recurring"]], ["Netflix"])
        n = r["recurring"][0]
        self.assertEqual((n["amount"], n["period"], n["last"], n["next"], n["category"]),
                         (12.99, "monthly", "2026-09-05", "2026-10-05", "abonnements"))
        self.assertEqual(r["monthly_total"], 12.99)

    def test_shared_and_settle(self):
        self.seed([self.row(1, "2026-10-02", -100, "COURSES"), self.row(2, "2026-10-03", -50, "RESTO"),
                   self.row(3, "2026-10-04", -30, "PERSO"), self.row(4, "2026-09-04", -30, "OLD")])
        ok("shared", {"transaction_id": "t1", "shared": True})  # payer: the account's owner
        ok("shared", {"transaction_id": "t2", "shared": True, "paid_by": "Marie"})
        s = ok("settle", {"month": "2026-10"})
        self.assertEqual((s["total"], s["count"]), (150, 2))
        people = {p["name"]: p for p in s["people"]}
        self.assertEqual((people["Jean-Baptiste"]["paid"], people["Marie"]["paid"]), (100, 50))
        self.assertEqual(s["transfers"], [{"from": "Marie", "to": "Jean-Baptiste", "amount": 25}])
        ok("shared", {"transaction_id": "t1", "shared": False})
        self.assertEqual(ok("settle", {"month": "2026-10"})["transfers"],
                         [{"from": "Jean-Baptiste", "to": "Marie", "amount": 25}])
        ok("shared", {"pattern": "perso", "shared": True, "paid_by": "Marie"})
        self.assertEqual(ok("settle", {"month": "2026-10"})["total"], 80)

    # ----- holdings

    def test_holdings_pricing_and_networth(self):
        self.serve({"/sessions": SESSION})
        ok("connected", {"code": "c"})
        server.write_json("sync.json", {UID: {"balance": 1000.0}})
        ok("holding_set", {"name": "CW8", "kind": "etf", "ticker": "CW8.PA", "quantity": 10})
        ok("holding_set", {"name": "Apple", "kind": "stock", "ticker": "AAPL", "quantity": 2})
        ok("holding_set", {"name": "BTC", "kind": "crypto", "ticker": "bitcoin", "quantity": 0.5})
        ok("holding_set", {"name": "SCPI Corum", "kind": "scpi", "value": 5000})
        self.assertTrue(call("holding_set", {"name": "x", "kind": "etf"})["isError"])
        self.assertTrue(call("holding_set", {"name": "x", "kind": "boat", "value": 1})["isError"])

        def yahoo(c):
            sym = urllib.parse.unquote(c[1].rsplit("/", 1)[1])
            assert c[4].get("User-agent")
            px = {"CW8.PA": (500, "EUR"), "AAPL": (220, "USD"), "EURUSD=X": (1.1, "USD")}[sym]
            return {"chart": {"result": [{"meta": {"regularMarketPrice": px[0], "currency": px[1]}}]}}
        api = self.serve({"finance.yahoo.com": yahoo, "coingecko": {"bitcoin": {"eur": 60000}}})
        n = ok("networth")
        lines = {l["name"]: l["value"] for l in n["lines"]}
        self.assertEqual(lines["CW8"], 5000)
        self.assertEqual(lines["Apple"], 400)
        self.assertEqual(lines["BTC"], 30000)
        self.assertEqual(lines["SCPI Corum"], 5000)
        self.assertEqual(lines["Fortuneo · Compte courant"], 1000)
        self.assertEqual(n["total"], 41400)
        self.assertEqual(n["by_kind"][0], {"kind": "crypto", "value": 30000, "share": 0.725})
        self.assertEqual(n["notes"], [])
        calls = len(api.calls)
        ok("networth")  # prices cached an hour
        self.assertEqual(len(api.calls), calls)
        ok("holding_remove", {"name": "btc"})
        self.assertEqual(ok("networth")["total"], 11400)
        self.assertTrue(call("holding_remove", {"name": "btc"})["isError"])

    # ----- protocol and manifest

    def test_protocol_and_manifest(self):
        with open(os.path.join(HERE, "extension.toml"), "rb") as f:
            manifest = tomllib.load(f)
        tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual({f'finance.{t["name"]}' for t in tools}, set(manifest["tools"]))
        self.assertEqual(manifest["tools"]["finance.disconnect"]["class"], "confirm")
        self.assertEqual({t["name"] for t in tools} & {"banks", "sync", "networth"}, {"banks", "sync", "networth"})
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(server.handle({"jsonrpc": "2.0", "id": 2, "method": "nope"})["error"]["code"], -32601)
        self.assertEqual(server.handle({"jsonrpc": "2.0", "id": 3, "method": "ping"})["result"], {})
        self.assertTrue(all(w["id"].startswith("finance-") for w in manifest["recipe"][0]["watch"]))


if __name__ == "__main__":
    unittest.main()
