#!/usr/bin/env python3
"""MCP stdio server of the `notion` extension (stdlib only).

Official Notion REST API, `Notion-Version: 2025-09-03` (databases hold "data
sources": queries and new rows go to a data source; a database id is resolved to its
first data source). Token from `$NOTION_TOKEN` (Secret Service item `notion`), never
logged nor echoed. Results are `structuredContent` plus the same JSON as text; a
failure is an `isError` result with a French sentence, never a crash. Nothing is logged.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = "2025-06-18"
API = "https://api.notion.com/v1"
VERSION = "2025-09-03"
TIMEOUT = 8          # seconds per request; the daemon's call deadline is 60 s
RETRY_CAP = 5        # longest Retry-After we wait (seconds), once
MAX_CHARS = 20000    # read_page text cap
MAX_REQUESTS = 25    # read_page block-children requests cap
CHUNK = 2000         # Notion's limit per rich_text content


class Failure(Exception):
    """A message for the model: the call failed, say why."""


NO_TOKEN = ("Pas de jeton Notion. L'utilisateur doit créer une intégration interne sur "
            "https://www.notion.so/profile/integrations (jeton « ntn_… »), la "
            "stocker avec `samantha provider set-key notion`, puis partager les pages "
            "voulues avec l'intégration (menu ••• de la page > Connexions).")
NOT_FOUND = ("Notion ne trouve pas cet objet: soit l'id est faux, soit la page ou la base "
             "n'est pas partagée avec l'intégration (menu ••• de la page > Connexions > "
             "ajouter l'intégration).")


def token():
    t = os.environ.get("NOTION_TOKEN", "").strip()
    if not t:
        raise Failure(NO_TOKEN)
    return t


def api(method, path, body=None, query=None, retry=True):
    url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
    req = urllib.request.Request(url, method=method, headers={
        "Authorization": "Bearer " + token(), "Notion-Version": VERSION,
        "Content-Type": "application/json", "Accept": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            with e:
                err = json.load(e)
        except (ValueError, AttributeError, OSError):
            err = {}
        code, message = err.get("code"), err.get("message") or e.reason
        if e.code == 429 and retry:
            try:
                wait = float((e.headers or {}).get("Retry-After") or 1)
            except (TypeError, ValueError):
                wait = 1.0
            time.sleep(max(0.0, min(RETRY_CAP, wait)))
            return api(method, path, body, query, retry=False)
        if code == "object_not_found" or e.code == 404:
            raise Failure(NOT_FOUND)
        if code == "unauthorized" or e.code == 401:
            raise Failure("Notion refuse le jeton (invalide ou révoqué): l'utilisateur doit "
                          "le recréer et le stocker avec `samantha provider set-key notion`.")
        if code == "restricted_resource" or e.code == 403:
            raise Failure(f"L'intégration n'a pas le droit de faire cela: {message} "
                          "(vérifier ses capacités sur notion.so/profile/integrations).")
        if e.code == 429:
            raise Failure("Notion limite le débit (trop de requêtes): réessayer dans un moment.")
        raise Failure(f"Notion a refusé la requête ({e.code}): {message}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise Failure(f"Notion injoignable (réseau ?): {getattr(e, 'reason', e)}")
    except ValueError:
        raise Failure("Notion a répondu quelque chose d'illisible")


def notion_id(value, what="id"):
    """A 32-hex Notion id, from an id (dashed or not) or a notion.so URL."""
    found = re.findall(r"[0-9a-fA-F]{32}", str(value or "").replace("-", ""))
    if not found:
        raise Failure(f"{what} Notion invalide: donne un id ou un lien notion.so")
    h = found[-1].lower()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def limit(args, default, top):
    return max(1, min(top, int(args.get("limit") or default)))


def text_arg(args, key):
    v = args.get(key)
    if not isinstance(v, str) or not v.strip():
        raise Failure(f"argument « {key} » manquant ou vide")
    return v.strip()


# --- shaping ---------------------------------------------------------------

def plain(rich):
    return "".join(r.get("plain_text") or r.get("text", {}).get("content", "")
                   for r in rich or [])


def title_of(obj):
    if isinstance(obj.get("title"), list):  # data source / database
        return plain(obj["title"])
    for p in (obj.get("properties") or {}).values():
        if p.get("type") == "title":
            return plain(p.get("title"))
    return ""


def simple(p):
    """A property value as a short scalar or list."""
    t = p.get("type")
    v = p.get(t)
    if t in ("title", "rich_text"):
        return plain(v)
    if t in ("select", "status"):
        return (v or {}).get("name")
    if t == "multi_select":
        return [o.get("name") for o in v or []]
    if t == "date":
        if not v:
            return None
        return v.get("start") + (" → " + v["end"] if v.get("end") else "")
    if t == "people":
        return [u.get("name") or u.get("id") for u in v or []]
    if t == "files":
        return [f.get("name") for f in v or []]
    if t == "relation":
        return [r.get("id") for r in v or []]
    if t == "formula":
        return (v or {}).get((v or {}).get("type"))
    if t == "rollup":
        v = v or {}
        inner = v.get(v.get("type"))
        return len(inner) if isinstance(inner, list) else inner
    if t == "unique_id":
        v = v or {}
        return f"{v.get('prefix') + '-' if v.get('prefix') else ''}{v.get('number')}"
    if t in ("created_by", "last_edited_by"):
        return (v or {}).get("name") or (v or {}).get("id")
    return v  # number, checkbox, url, email, phone_number, *_time…


def properties(obj):
    return {k: simple(p) for k, p in (obj.get("properties") or {}).items()
            if p.get("type") != "title"}


def kind(obj):
    return "database" if obj.get("object") in ("data_source", "database") else "page"


PREFIX = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### ",
          "bulleted_list_item": "- ", "numbered_list_item": "1. ", "quote": "> ",
          "callout": "> ", "toggle": "▸ "}


def block_text(b):
    t = b.get("type")
    v = b.get(t) or {}
    if t in PREFIX:
        return PREFIX[t] + plain(v.get("rich_text"))
    if t == "to_do":
        return ("[x] " if v.get("checked") else "[ ] ") + plain(v.get("rich_text"))
    if t == "code":
        return f"```{v.get('language') or ''}\n{plain(v.get('rich_text'))}\n```"
    if t == "divider":
        return "---"
    if t == "child_page":
        return f"[page: {v.get('title', '')}]"
    if t == "child_database":
        return f"[base: {v.get('title', '')}]"
    if t == "equation":
        return v.get("expression", "")
    if t == "table_row":
        return " | ".join(plain(c) for c in v.get("cells") or [])
    if t in ("bookmark", "embed", "link_preview"):
        return v.get("url", "")
    if t in ("image", "video", "file", "pdf", "audio"):
        cap = plain(v.get("caption"))
        return f"[{t}{': ' + cap if cap else ''}]"
    if "rich_text" in v:
        return plain(v["rich_text"])
    return ""


# --- tools -----------------------------------------------------------------

def search(args):
    query = text_arg(args, "query")
    body = {"query": query, "page_size": limit(args, 10, 20),
            "sort": {"direction": "descending", "timestamp": "last_edited_time"}}
    f = args.get("filter")
    if f:
        if f not in ("page", "database"):
            raise Failure("filter doit être « page » ou « database »")
        body["filter"] = {"property": "object",
                          "value": "data_source" if f == "database" else "page"}
    data = api("POST", "/search", body)
    return {"rows": [{"id": o.get("id"), "title": title_of(o), "url": o.get("url"),
                      "type": kind(o), "last_edited": o.get("last_edited_time")}
                     for o in data.get("results") or []]}


def children(block_id, state):
    """All children of a block, paginated, within the request budget."""
    out, cursor = [], None
    while True:
        if state["requests"] >= MAX_REQUESTS:
            state["truncated"] = True
            return out
        state["requests"] += 1
        q = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
        data = api("GET", f"/blocks/{block_id}/children", query=q)
        out += data.get("results") or []
        cursor = data.get("next_cursor")
        if not data.get("has_more") or not cursor:
            return out


def read_page(args):
    pid = notion_id(args.get("id"))
    page = api("GET", f"/pages/{pid}")
    state = {"requests": 0, "truncated": False}
    lines, size = [], 0

    def add(line):
        nonlocal size
        if size + len(line) + 1 > MAX_CHARS:
            state["truncated"] = True
            return False
        lines.append(line)
        size += len(line) + 1
        return True

    for b in children(pid, state):
        if not add(block_text(b)):
            break
        if b.get("has_children") and b.get("type") not in ("child_page", "child_database"):
            # ponytail: one nested level only; deeper content is listed as truncated.
            full = False
            for c in children(b["id"], state):
                if c.get("has_children"):
                    state["truncated"] = True
                if not add("  " + block_text(c)):
                    full = True
                    break
            if full:
                break
    return {"id": page.get("id"), "title": title_of(page), "url": page.get("url"),
            "last_edited": page.get("last_edited_time"), "properties": properties(page),
            "content": "\n".join(lines), "truncated": state["truncated"]}


def data_source(any_id):
    """(data_source_id, schema) from a data source id or a database id."""
    did = notion_id(any_id)
    try:
        ds = api("GET", f"/data_sources/{did}")
    except Failure as e:
        if str(e) != NOT_FOUND:
            raise
        db = api("GET", f"/databases/{did}")
        sources = db.get("data_sources") or []
        if not sources:
            raise Failure("Cette base Notion n'a aucune source de données accessible")
        ds = api("GET", f"/data_sources/{sources[0]['id']}")
    return ds["id"], ds.get("properties") or {}


def query_database(args):
    ds_id, _ = data_source(args.get("id"))
    body = {"page_size": limit(args, 20, 50)}
    f = args.get("filter")
    if f:
        if not isinstance(f, dict):
            raise Failure("filter doit être un objet JSON de filtre Notion")
        body["filter"] = f
    data = api("POST", f"/data_sources/{ds_id}/query", body)
    rows = [{"id": o.get("id"), "title": title_of(o), "url": o.get("url"),
             "last_edited": o.get("last_edited_time"), "properties": properties(o)}
            for o in data.get("results") or []]
    return {"data_source_id": ds_id, "rows": rows, "has_more": bool(data.get("has_more"))}


def rich(text):
    return [{"type": "text", "text": {"content": text[i:i + CHUNK]}}
            for i in range(0, len(text), CHUNK)]


def paragraphs(text):
    return [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich(p)}}
            for p in (line.strip() for line in text.splitlines()) if p]


def append_blocks(page_id, blocks):
    for i in range(0, len(blocks), 100):  # Notion takes 100 children per request
        api("PATCH", f"/blocks/{page_id}/children", {"children": blocks[i:i + 100]})


def create_page(args):
    title = text_arg(args, "title")
    kind_ = args.get("parent_type")
    if kind_ == "page":
        parent = {"type": "page_id", "page_id": notion_id(args.get("parent_id"), "parent_id")}
        props = {"title": {"title": rich(title)}}
    elif kind_ == "database":
        ds_id, schema = data_source(args.get("parent_id"))
        name = next((k for k, p in schema.items() if p.get("type") == "title"), "Name")
        parent = {"type": "data_source_id", "data_source_id": ds_id}
        props = {name: {"title": rich(title)}}
    else:
        raise Failure("parent_type doit être « page » ou « database »")
    blocks = paragraphs(args.get("content") or "") if isinstance(args.get("content"), str) else []
    page = api("POST", "/pages", {"parent": parent, "properties": props,
                                  "children": blocks[:100]})
    append_blocks(page["id"], blocks[100:])
    return {"id": page["id"], "url": page.get("url"), "title": title,
            "paragraphs": len(blocks)}


def append(args):
    pid = notion_id(args.get("page_id"), "page_id")
    blocks = paragraphs(text_arg(args, "text"))
    append_blocks(pid, blocks)
    return {"page_id": pid, "appended": len(blocks)}


def comment(args):
    pid = notion_id(args.get("page_id"), "page_id")
    c = api("POST", "/comments", {"parent": {"page_id": pid},
                                  "rich_text": rich(text_arg(args, "text"))})
    return {"id": c.get("id"), "page_id": pid}


STR = {"type": "string"}
ID = {"type": "string", "description": "Notion id (with or without dashes) or notion.so link."}
ROWS = {"type": "array", "items": {"type": "object"}}
TOOLS = {
    "search": (search, {
        "description": "Search pages and databases shared with the integration, by title.",
        "inputSchema": {"type": "object", "required": ["query"], "properties": {
            "query": STR, "filter": {"type": "string", "enum": ["page", "database"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20}}},
        "outputSchema": {"type": "object", "properties": {"rows": {"type": "array", "items": {
            "type": "object", "properties": {"id": STR, "title": STR, "url": STR,
                                             "type": STR, "last_edited": STR}}}}},
    }),
    "read_page": (read_page, {
        "description": "A page's title, properties and content as plain text (capped).",
        "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": ID}},
        "outputSchema": {"type": "object", "properties": {
            "id": STR, "title": STR, "url": STR, "last_edited": STR,
            "properties": {"type": "object"}, "content": STR,
            "truncated": {"type": "boolean"}}},
    }),
    "query_database": (query_database, {
        "description": "Rows of a database (or data source) with simplified properties; "
                       "optional Notion filter object passed through.",
        "inputSchema": {"type": "object", "required": ["id"], "properties": {
            "id": ID, "filter": {"type": "object"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50}}},
        "outputSchema": {"type": "object", "properties": {
            "data_source_id": STR, "rows": ROWS, "has_more": {"type": "boolean"}}},
    }),
    "create_page": (create_page, {
        "description": "Create a page under a page or as a row of a database. "
                       "The user confirms.",
        "inputSchema": {"type": "object", "required": ["parent_id", "parent_type", "title"],
                        "properties": {
                            "parent_id": ID,
                            "parent_type": {"type": "string", "enum": ["page", "database"]},
                            "title": STR,
                            "content": {"type": "string",
                                        "description": "Plain text, one paragraph per line."}}},
    }),
    "append": (append, {
        "description": "Append plain-text paragraphs (one per line) to a page. "
                       "The user confirms.",
        "inputSchema": {"type": "object", "required": ["page_id", "text"],
                        "properties": {"page_id": ID, "text": STR}},
    }),
    "comment": (comment, {
        "description": "Add a comment to a page. The user confirms.",
        "inputSchema": {"type": "object", "required": ["page_id", "text"],
                        "properties": {"page_id": ID, "text": STR}},
    }),
}


def call(name, args):
    run = TOOLS[name][0]
    try:
        out = run(args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Réponse de Notion inattendue "
                             "ou argument invalide"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out, "isError": False}


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None  # notification
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "samantha-notion", "version": "0.1.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": n, **spec} for n, (_, spec) in TOOLS.items()]}
    elif method == "tools/call" and params.get("name") in TOOLS:
        result = call(params["name"], params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": "method not found"}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            reply = handle(json.loads(line))
        except (ValueError, AttributeError):
            reply = {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": "parse error"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
