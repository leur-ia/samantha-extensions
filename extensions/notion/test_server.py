"""python3 -m unittest -q (from contrib/notion)"""

import io
import json
import os
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import server

PAGE = "1" * 32
DB = "2" * 32
DS = "3" * 32
TOKEN = "ntn_secret_value_123"


def dashed(h):
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def rt(s):
    return [{"type": "text", "plain_text": s, "text": {"content": s}}]


def para(s, has_children=False, bid="b"):
    return {"object": "block", "id": bid, "type": "paragraph", "has_children": has_children,
            "paragraph": {"rich_text": rt(s)}}


def http_error(url, code, body, headers=None):
    return urllib.error.HTTPError(url, code, "err", headers or {},
                                  io.BytesIO(json.dumps(body).encode()))


class Notion:
    """Stands in for urlopen: routes "METHOD /path" to a body, a callable or an
    exception; records (method, path, query, body, headers)."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, req, timeout=None):
        assert timeout, "every request has a timeout"
        u = urllib.parse.urlparse(req.full_url)
        path = u.path.removeprefix("/v1")
        body = json.loads(req.data) if req.data else None
        query = dict(urllib.parse.parse_qsl(u.query))
        self.calls.append((req.get_method(), path, query, body, dict(req.header_items())))
        answer = self.routes[f"{req.get_method()} {path}"]
        if callable(answer):
            answer = answer(query, body)
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(json.dumps(answer).encode())


def call(name, arguments):
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments}})["result"]


class Base(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {"NOTION_TOKEN": TOKEN})
        p.start()
        self.addCleanup(p.stop)
        s = mock.patch.object(server.time, "sleep")
        self.sleep = s.start()
        self.addCleanup(s.stop)

    def serve(self, routes):
        fake = Notion(routes)
        p = mock.patch.object(server.urllib.request, "urlopen", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def assertError(self, r, needle):
        self.assertTrue(r["isError"], r)
        self.assertIn(needle, r["content"][0]["text"])
        self.assertNotIn(TOKEN, r["content"][0]["text"])


class Tools(Base):
    def test_search_shapes_and_maps_filter(self):
        fake = self.serve({"POST /search": {"results": [
            {"object": "page", "id": dashed(PAGE), "url": "https://notion.so/p",
             "last_edited_time": "2026-10-01T10:00:00.000Z",
             "properties": {"Nom": {"type": "title", "title": rt("Courses")}}},
            {"object": "data_source", "id": dashed(DS), "url": "https://notion.so/d",
             "last_edited_time": "2026-09-01T10:00:00.000Z", "title": rt("Tâches")}]}})
        r = call("search", {"query": "co", "filter": "database", "limit": 99})
        self.assertFalse(r["isError"])
        self.assertEqual(r["structuredContent"]["rows"], [
            {"id": dashed(PAGE), "title": "Courses", "url": "https://notion.so/p",
             "type": "page", "last_edited": "2026-10-01T10:00:00.000Z"},
            {"id": dashed(DS), "title": "Tâches", "url": "https://notion.so/d",
             "type": "database", "last_edited": "2026-09-01T10:00:00.000Z"}])
        method, path, _, body, headers = fake.calls[0]
        self.assertEqual(body["filter"], {"property": "object", "value": "data_source"})
        self.assertEqual(body["page_size"], 20)
        self.assertEqual(headers["Notion-version"], server.VERSION)
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        self.assertError(call("search", {"query": "x", "filter": "user"}), "filter")
        self.assertError(call("search", {}), "query")

    def test_read_page_paginates_nests_one_level(self):
        def kids(query, _):
            if query.get("start_cursor") == "c2":
                return {"results": [para("deux", has_children=True, bid="n")],
                        "has_more": False, "next_cursor": None}
            return {"results": [
                {"type": "heading_1", "id": "h", "heading_1": {"rich_text": rt("Titre")}},
                {"type": "to_do", "id": "t", "to_do": {"rich_text": rt("lait"), "checked": True}},
                {"type": "child_page", "id": "cp", "has_children": True,
                 "child_page": {"title": "Sous-page"}}],
                "has_more": True, "next_cursor": "c2"}
        fake = self.serve({
            f"GET /pages/{dashed(PAGE)}": {
                "id": dashed(PAGE), "url": "https://notion.so/p",
                "last_edited_time": "2026-10-01T10:00:00.000Z",
                "properties": {"Nom": {"type": "title", "title": rt("Courses")},
                               "Tags": {"type": "multi_select",
                                        "multi_select": [{"name": "maison"}]},
                               "Statut": {"type": "status", "status": {"name": "En cours"}}}},
            f"GET /blocks/{dashed(PAGE)}/children": kids,
            "GET /blocks/n/children": {"results": [para("enfant", has_children=True)],
                                       "has_more": False}})
        r = call("read_page", {"id": f"https://www.notion.so/Courses-{PAGE}"})
        out = r["structuredContent"]
        self.assertEqual(out["title"], "Courses")
        self.assertEqual(out["properties"], {"Tags": ["maison"], "Statut": "En cours"})
        self.assertEqual(out["content"],
                         "# Titre\n[x] lait\n[page: Sous-page]\ndeux\n  enfant")
        self.assertTrue(out["truncated"])  # the grandchild was not fetched
        paths = [c[1] for c in fake.calls]
        self.assertNotIn("/blocks/cp/children", paths)
        self.assertEqual(paths.count(f"/blocks/{dashed(PAGE)}/children"), 2)

    def test_read_page_caps_size(self):
        big = "x" * 1000
        self.serve({
            f"GET /pages/{dashed(PAGE)}": {"id": dashed(PAGE), "properties": {}},
            f"GET /blocks/{dashed(PAGE)}/children": {
                "results": [para(big) for _ in range(50)], "has_more": False}})
        out = call("read_page", {"id": PAGE})["structuredContent"]
        self.assertTrue(out["truncated"])
        self.assertLessEqual(len(out["content"]), server.MAX_CHARS)

    def test_query_database_resolves_database_to_data_source(self):
        not_found = http_error("u", 404, {"object": "error", "code": "object_not_found",
                                          "message": "Could not find"})
        fake = self.serve({
            f"GET /data_sources/{dashed(DB)}": not_found,
            f"GET /databases/{dashed(DB)}": {"data_sources": [{"id": dashed(DS), "name": "T"}]},
            f"GET /data_sources/{dashed(DS)}": {"id": dashed(DS), "properties": {}},
            f"POST /data_sources/{dashed(DS)}/query": {"has_more": True, "results": [
                {"object": "page", "id": "r1", "url": "u1", "last_edited_time": "t",
                 "properties": {"Nom": {"type": "title", "title": rt("Écrire")},
                                "Fait": {"type": "checkbox", "checkbox": False},
                                "Échéance": {"type": "date", "date": {"start": "2026-10-05",
                                                                      "end": None}},
                                "Points": {"type": "number", "number": 3}}}]}})
        flt = {"property": "Fait", "checkbox": {"equals": False}}
        r = call("query_database", {"id": DB, "filter": flt, "limit": 500})
        out = r["structuredContent"]
        self.assertEqual(out["data_source_id"], dashed(DS))
        self.assertTrue(out["has_more"])
        self.assertEqual(out["rows"][0], {"id": "r1", "title": "Écrire", "url": "u1",
                                          "last_edited": "t", "properties": {
                                              "Fait": False, "Échéance": "2026-10-05",
                                              "Points": 3}})
        self.assertEqual(fake.calls[-1][3], {"page_size": 50, "filter": flt})
        self.assertError(call("query_database", {"id": DS, "filter": "Fait=0"}), "filter")

    def test_create_page_under_page_and_in_database(self):
        fake = self.serve({"POST /pages": {"id": "new", "url": "https://notion.so/new"},
                           "PATCH /blocks/new/children": {"results": []}})
        content = "\n".join(f"ligne {i}" for i in range(150)) + "\n\n"
        r = call("create_page", {"parent_id": PAGE, "parent_type": "page",
                                 "title": "Notes", "content": content})
        self.assertEqual(r["structuredContent"]["paragraphs"], 150)
        body = fake.calls[0][3]
        self.assertEqual(body["parent"], {"type": "page_id", "page_id": dashed(PAGE)})
        self.assertEqual(body["properties"]["title"]["title"][0]["text"]["content"], "Notes")
        self.assertEqual(len(body["children"]), 100)
        self.assertEqual(len(fake.calls[1][3]["children"]), 50)

        fake = self.serve({
            f"GET /data_sources/{dashed(DS)}": {"id": dashed(DS), "properties": {
                "Tâche": {"type": "title"}, "Fait": {"type": "checkbox"}}},
            "POST /pages": {"id": "row", "url": "u"}})
        call("create_page", {"parent_id": DS, "parent_type": "database", "title": "Appeler"})
        body = fake.calls[-1][3]
        self.assertEqual(body["parent"], {"type": "data_source_id",
                                          "data_source_id": dashed(DS)})
        self.assertIn("Tâche", body["properties"])
        self.assertError(call("create_page", {"parent_id": PAGE, "parent_type": "x",
                                              "title": "a"}), "parent_type")

    def test_append_and_comment(self):
        fake = self.serve({f"PATCH /blocks/{dashed(PAGE)}/children": {"results": []},
                           "POST /comments": {"id": "c1"}})
        long = "a" * 4500
        r = call("append", {"page_id": PAGE, "text": f"un\n\n{long}"})
        self.assertEqual(r["structuredContent"], {"page_id": dashed(PAGE), "appended": 2})
        blocks = fake.calls[0][3]["children"]
        self.assertEqual([len(t["text"]["content"]) for t in blocks[1]["paragraph"]["rich_text"]],
                         [2000, 2000, 500])
        r = call("comment", {"page_id": PAGE, "text": "Vu !"})
        self.assertEqual(r["structuredContent"], {"id": "c1", "page_id": dashed(PAGE)})
        self.assertEqual(fake.calls[1][3]["parent"], {"page_id": dashed(PAGE)})
        self.assertError(call("append", {"page_id": PAGE, "text": " "}), "text")
        self.assertError(call("comment", {"page_id": "pas-un-id", "text": "x"}), "invalide")


class Errors(Base):
    def test_missing_token(self):
        fake = self.serve({})
        with mock.patch.dict(os.environ, {"NOTION_TOKEN": ""}):
            for name, args in [("search", {"query": "x"}), ("read_page", {"id": PAGE})]:
                self.assertError(call(name, args), "samantha provider set-key notion")
        self.assertEqual(fake.calls, [])

    def test_not_found_explains_connections(self):
        self.serve({f"GET /pages/{dashed(PAGE)}": http_error("u", 404, {
            "object": "error", "status": 404, "code": "object_not_found",
            "message": "Could not find page"})})
        self.assertError(call("read_page", {"id": PAGE}), "Connexions")

    def test_other_notion_errors_surface_message(self):
        self.serve({"POST /search": http_error("u", 400, {
            "code": "validation_error", "message": "body.query should be a string"})})
        self.assertError(call("search", {"query": "x"}), "body.query should be a string")
        self.serve({"POST /search": http_error("u", 401, {"code": "unauthorized",
                                                          "message": "API token is invalid."})})
        self.assertError(call("search", {"query": "x"}), "set-key notion")

    def test_rate_limit_retried_once(self):
        answers = [http_error("u", 429, {"code": "rate_limited", "message": "slow"},
                              {"Retry-After": "120"}),
                   {"results": []}]
        fake = self.serve({"POST /search": lambda q, b: answers.pop(0)})
        self.assertFalse(call("search", {"query": "x"})["isError"])
        self.sleep.assert_called_once_with(server.RETRY_CAP)
        self.assertEqual(len(fake.calls), 2)

        self.serve({"POST /search": lambda q, b: http_error(
            "u", 429, {"code": "rate_limited", "message": "slow"}, {"Retry-After": "1"})})
        self.assertError(call("search", {"query": "x"}), "débit")

    def test_network_down(self):
        def down(req, timeout=None):
            raise urllib.error.URLError("Name or service not known")
        with mock.patch.object(server.urllib.request, "urlopen", down):
            self.assertError(call("search", {"query": "x"}), "injoignable")

    def test_protocol(self):
        tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual([t["name"] for t in tools["result"]["tools"]],
                         ["search", "read_page", "query_database", "create_page",
                          "append", "comment"])
        self.assertIsNone(server.handle({"jsonrpc": "2.0",
                                         "method": "notifications/initialized"}))
        self.assertEqual(server.handle({"jsonrpc": "2.0", "id": 2,
                                        "method": "nope"})["error"]["code"], -32601)
        init = server.handle({"jsonrpc": "2.0", "id": 3, "method": "initialize"})
        self.assertEqual(init["result"]["protocolVersion"], server.PROTOCOL)


if __name__ == "__main__":
    unittest.main()
