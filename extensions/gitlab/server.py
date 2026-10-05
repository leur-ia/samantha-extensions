#!/usr/bin/env python3
"""MCP stdio server of the `gitlab` extension (stdlib only).

A provider of Samantha's `code` front: the GitLab REST API (gitlab.com, or the host in
~/.config/samantha/gitlab.toml: `host = "https://gitlab.example.org"`) with the user's
own personal access token (GITLAB_TOKEN, Secret Service item `gitlab`; scope read_api,
or api to mark to-dos done). Methods: inbox (to-dos), reviews, mine, repos, ci,
mark_read. Nothing is logged; no error carries the token.
"""

import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = "2025-06-18"
TIMEOUT = 15
PROJECT = re.compile(r"^(?!.*(^|/)\.\.?(/|$))[\w.-]+(/[\w.-]+)+$")
KINDS = {"MergeRequest": "mr", "Issue": "issue", "Commit": "commit", "Epic": "epic", "DesignManagement::Design": "design"}
SETUP = ("GitLab n'est pas configuré: crée un jeton d'accès personnel (GitLab > Préférences > Jetons "
         "d'accès, portée read_api ou api) et enregistre-le avec `samantha provider set-key gitlab`.")

_me = {}


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def host():
    home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    try:
        with open(os.path.join(home, "samantha", "gitlab.toml"), "rb") as f:
            h = tomllib.load(f).get("host")
    except (OSError, tomllib.TOMLDecodeError):
        h = None
    h = h if isinstance(h, str) and h.startswith("https://") else "https://gitlab.com"
    return h.rstrip("/")


def api(method, path, query=None):
    token = os.environ.get("GITLAB_TOKEN", "").strip()
    if not token:
        raise Failure(SETUP)
    url = f"{host()}/api/v4{path}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
    req = urllib.request.Request(url, method=method, headers={"PRIVATE-TOKEN": token, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise Failure(SETUP)
        if e.code in (403, 404):
            raise Failure("GitLab refuse l'accès (portée du jeton insuffisante, ou projet introuvable)")
        raise Failure(f"GitLab a refusé la requête ({e.code})")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise Failure("GitLab injoignable (réseau ?)")
    except ValueError:
        raise Failure("GitLab a répondu quelque chose d'illisible")


def limit_of(args, default=20):
    return max(1, min(50, int(args.get("limit") or default)))


def me():
    if "username" not in _me:
        _me["username"] = api("GET", "/user")["username"]
    return _me["username"]


def inbox(args):
    got = api("GET", "/todos", {"state": "pending", "per_page": limit_of(args)})
    return {"items": [{"id": str(t["id"]), "kind": KINDS.get(t.get("target_type"), "other"),
                       "title": (t.get("target") or {}).get("title") or t.get("body", ""),
                       "repo": (t.get("project") or {}).get("path_with_namespace", ""),
                       "reason": t.get("action_name", ""), "author": (t.get("author") or {}).get("username", ""),
                       "updated": t.get("updated_at") or t.get("created_at", ""), "url": t.get("target_url", ""),
                       "unread": True} for t in got]}


def merge_requests(query, args):
    got = api("GET", "/merge_requests", {"state": "opened", "per_page": limit_of(args), "order_by": "updated_at", **query})
    items = []
    for m in got:
        repo = (m.get("references") or {}).get("full", "").rsplit("!", 1)[0] or str(m.get("project_id", ""))
        items.append({"id": f"{repo}!{m['iid']}", "kind": "mr", "title": m.get("title", ""), "repo": repo,
                      "author": (m.get("author") or {}).get("username", ""), "updated": m.get("updated_at", ""),
                      "url": m.get("web_url", "")})
    return {"items": items}


def reviews(args):
    return merge_requests({"scope": "all", "reviewer_username": me()}, args)


def mine(args):
    return merge_requests({"scope": "created_by_me"}, args)


def repos(args):
    got = api("GET", "/projects", {"membership": "true", "order_by": "last_activity_at", "per_page": limit_of(args)})
    return {"repos": [{"repo": p["path_with_namespace"], "description": p.get("description") or "",
                       "updated": p.get("last_activity_at", ""), "url": p.get("web_url", ""),
                       "private": p.get("visibility") != "public"} for p in got]}


def ci(args):
    repo = str(args.get("repo") or "")
    if not PROJECT.match(repo):
        raise Failure("repo: groupe/projet (de code.repos)")
    got = api("GET", f"/projects/{urllib.parse.quote(repo, safe='')}/pipelines", {"per_page": limit_of(args, 5)})
    return {"repo": repo, "runs": [{"name": f"pipeline #{p.get('iid') or p.get('id')}", "status": p.get("status", ""),
                                    "branch": p.get("ref", ""), "updated": p.get("updated_at", ""),
                                    "url": p.get("web_url", "")} for p in got]}


def mark_read(args):
    tid = str(args.get("id") or "")
    if not tid.isdigit():
        raise Failure("id: celui d'un to-do de code.inbox")
    api("POST", f"/todos/{tid}/mark_as_done")
    return {"done": tid}


I = {"type": "integer"}
LIM = {"type": "object", "properties": {"limit": I}}
TOOLS = {
    "inbox": (inbox, "GitLab to-dos.", LIM),
    "reviews": (reviews, "Merge requests waiting for the user's review.", LIM),
    "mine": (mine, "The user's open merge requests.", LIM),
    "repos": (repos, "The user's projects, most recently active first.", LIM),
    "ci": (ci, "Latest pipelines of a project.",
           {"type": "object", "required": ["repo"], "properties": {"repo": {"type": "string"}, "limit": I}}),
    "mark_read": (mark_read, "Mark a to-do done.",
                  {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Réponse de GitLab inattendue ou argument invalide"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out, "isError": False}


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None  # notification
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "samantha-gitlab", "version": "0.1.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": n, "description": d, "inputSchema": s} for n, (_, d, s) in TOOLS.items()]}
    elif method == "tools/call" and params.get("name") in TOOLS:
        result = call(params["name"], params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "method not found"}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            reply = handle(json.loads(line))
        except (ValueError, AttributeError):
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
