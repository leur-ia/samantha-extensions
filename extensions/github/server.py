#!/usr/bin/env python3
"""MCP stdio server of the `github` extension (stdlib only).

A provider of Samantha's `code` front: the GitHub REST API with the user's own token
(GITHUB_TOKEN, Secret Service item `github`: a fine-grained or classic token with
notifications and repository read access). Methods: inbox, reviews, mine, repos, ci,
mark_read. Nothing is logged; no error carries the token.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = "2025-06-18"
API = "https://api.github.com"
TIMEOUT = 15
# owner/name; a name may start with a dot (.github), but isn't . or ..
REPO = re.compile(r"^[A-Za-z0-9][\w-]*/(?!\.\.?$)[\w.-]+$")
KINDS = {"PullRequest": "pr", "Issue": "issue", "Release": "release", "CheckSuite": "ci",
         "Commit": "commit", "Discussion": "discussion", "WorkflowRun": "ci"}
SETUP = ("GitHub n'est pas configuré: crée un jeton (github.com > Settings > Developer settings > "
         "Personal access tokens, accès notifications et dépôts en lecture) et enregistre-le avec "
         "`samantha provider set-key github`.")


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def api(method, path, query=None):
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise Failure(SETUP)
    url = API + path + (f"?{urllib.parse.urlencode(query)}" if query else "")
    req = urllib.request.Request(url, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "samantha-github"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise Failure(SETUP)
        if e.code in (403, 404):
            raise Failure("GitHub refuse l'accès (jeton sans cette permission, ou dépôt introuvable)")
        raise Failure(f"GitHub a refusé la requête ({e.code})")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise Failure("GitHub injoignable (réseau ?)")
    except ValueError:
        raise Failure("GitHub a répondu quelque chose d'illisible")


def limit_of(args, default=20):
    return max(1, min(50, int(args.get("limit") or default)))


def html_url(subject, repo):
    """A notification's page: the API url's number, on github.com."""
    api_url = subject.get("url") or ""
    number = api_url.rstrip("/").rsplit("/", 1)[-1]
    kind = subject.get("type")
    if kind == "PullRequest" and number.isdigit():
        return f"https://github.com/{repo}/pull/{number}"
    if kind == "Issue" and number.isdigit():
        return f"https://github.com/{repo}/issues/{number}"
    return f"https://github.com/{repo}"


def inbox(args):
    got = api("GET", "/notifications", {"per_page": limit_of(args)})
    return {"items": [{"id": n["id"], "kind": KINDS.get((n.get("subject") or {}).get("type"), "other"),
                       "title": (n.get("subject") or {}).get("title", ""),
                       "repo": (n.get("repository") or {}).get("full_name", ""), "reason": n.get("reason", ""),
                       "updated": n.get("updated_at", ""), "unread": n.get("unread", True),
                       "url": html_url(n.get("subject") or {}, (n.get("repository") or {}).get("full_name", ""))}
                      for n in got]}


def search(q, args):
    got = api("GET", "/search/issues", {"q": q, "sort": "updated", "order": "desc", "per_page": limit_of(args)})
    items = []
    for i in got.get("items", []):
        repo = "/".join(i.get("repository_url", "").rsplit("/", 2)[-2:])
        items.append({"id": f"{repo}#{i['number']}", "kind": "pr", "title": i.get("title", ""), "repo": repo,
                      "author": (i.get("user") or {}).get("login", ""), "updated": i.get("updated_at", ""),
                      "url": i.get("html_url", "")})
    return {"items": items}


def reviews(args):
    return search("is:open is:pr review-requested:@me archived:false", args)


def mine(args):
    return search("is:open is:pr author:@me archived:false", args)


def repos(args):
    got = api("GET", "/user/repos", {"sort": "pushed", "per_page": limit_of(args)})
    return {"repos": [{"repo": r["full_name"], "description": r.get("description") or "", "updated": r.get("pushed_at", ""),
                       "url": r.get("html_url", ""), "private": r.get("private", False)} for r in got]}


def ci(args):
    repo = str(args.get("repo") or "")
    if not REPO.match(repo):
        raise Failure("repo: owner/nom (de code.repos)")
    got = api("GET", f"/repos/{repo}/actions/runs", {"per_page": limit_of(args, 5)})
    return {"repo": repo, "runs": [{"name": r.get("name", ""), "status": r.get("conclusion") or r.get("status", ""),
                                    "branch": r.get("head_branch", ""), "updated": r.get("updated_at", ""),
                                    "url": r.get("html_url", "")} for r in got.get("workflow_runs", [])]}


def mark_read(args):
    tid = str(args.get("id") or "")
    if not tid.isdigit():
        raise Failure("id: celui d'une notification de code.inbox")
    api("PATCH", f"/notifications/threads/{tid}")
    return {"read": tid}


I = {"type": "integer"}
LIM = {"type": "object", "properties": {"limit": I}}
TOOLS = {
    "inbox": (inbox, "GitHub notifications.", LIM),
    "reviews": (reviews, "Pull requests waiting for the user's review.", LIM),
    "mine": (mine, "The user's open pull requests.", LIM),
    "repos": (repos, "The user's repositories, most recently pushed first.", LIM),
    "ci": (ci, "Latest GitHub Actions runs of a repository.",
           {"type": "object", "required": ["repo"], "properties": {"repo": {"type": "string"}, "limit": I}}),
    "mark_read": (mark_read, "Mark a notification read.",
                  {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Réponse de GitHub inattendue ou argument invalide"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out, "isError": False}


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None  # notification
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "samantha-github", "version": "0.1.0"}}
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
