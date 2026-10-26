#!/usr/bin/env python3
"""MCP stdio server of the `reddit` extension (stdlib only).

A provider of Otto's `social` front on Reddit's official OAuth2 API. Accounts in
~/.config/otto/social.toml (other providers' tables are ignored):

    [reddit.perso]
    username = "my_reddit_name"
    client_id = "the-script-app-client-id"   # reddit.com/prefs/apps, type "script"

The Secret Service item social-<name> holds `<client secret>:<account password>`,
split on the first colon, asked of the daemon (`secrets.get`) at use and never
logged. A "script" app's password grant gives a bearer token, cached until it expires.
Ids are `<account>/<fullname>` (t3_… posts, t1_… comments); the daemon adds `reddit:`.
Failures are `isError` results, never a crash; no error carries a secret.
"""

import base64
import datetime
import json
import os
import re
import socket
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = "2025-06-18"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API = "https://oauth.reddit.com"
SITE = "https://reddit.com"
TIMEOUT = 15
EXCERPT = 500
NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
FULLNAME = re.compile(r"t[13]_[a-z0-9]{1,13}")
SUBREDDIT = re.compile(r"r/([A-Za-z0-9_]{2,21})")
SETUP = ("Reddit n'est pas configuré: crée une application de type « script » sur "
         "reddit.com/prefs/apps (redirect uri http://localhost:8080), ajoute dans "
         "~/.config/otto/social.toml une table [reddit.<nom>] avec username = \"ton pseudo "
         "Reddit\" et client_id = \"l'identifiant affiché sous le nom de l'app\", puis "
         "enregistre `<secret de l'app>:<mot de passe du compte>` (séparés par deux-points) "
         "avec `otto provider set-key social-<nom>`. Le compte ne doit pas avoir la double "
         "authentification. Seule l'API officielle de Reddit est utilisée.")

TOKENS = {}                               # account name -> (token, expiry time)
RATE = {"remaining": None, "reset": 0.0}  # from the last X-Ratelimit-* headers


class Failure(Exception):
    """A message for the model: the call failed, say why."""


# --- accounts and secrets -------------------------------------------------------

def config_path():
    if os.environ.get("SOCIAL_CONFIG"):
        return os.environ["SOCIAL_CONFIG"]
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "otto", "social.toml")


def load_config():
    """{name: {name, username, client_id}} from the [reddit.<name>] tables."""
    try:
        with open(config_path(), "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise Failure(SETUP) from None
    except (OSError, tomllib.TOMLDecodeError):
        raise Failure(f"Impossible de lire {config_path()} (TOML invalide ?)") from None
    accounts = {}
    for name, a in (raw.get("reddit") or {}).items():
        if not isinstance(a, dict) or not a.get("username") or not a.get("client_id"):
            raise Failure(f"[reddit.{name}] doit avoir username et client_id. " + SETUP)
        if not NAME.fullmatch(name):
            raise Failure(f"Nom de compte {name!r}: lettres minuscules, chiffres et tirets")
        accounts[name] = {"name": name, "username": str(a["username"]).removeprefix("u/"),
                          "client_id": str(a["client_id"])}
    if not accounts:
        raise Failure(SETUP)
    return accounts


def pick(accounts, name):
    """One account by name (`reddit:` prefix tolerated)."""
    name = (name or "").removeprefix("reddit:")
    if name not in accounts:
        raise Failure(f"Pas de compte Reddit {name!r}; comptes: {', '.join(accounts)}")
    return accounts[name]


def chosen(accounts, name):
    """The accounts a read covers: the one named, or all."""
    return [pick(accounts, name)] if name else list(accounts.values())


def daemon(method, params, timeout=30):
    """One request on the daemon socket under the `extension` role."""
    path, token = os.environ.get("OTTO_SOCKET"), os.environ.get("OTTO_EXTENSION_TOKEN")
    if not path or not token:
        raise Failure("Le démon d'Otto n'est pas joignable")
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(timeout)
        s.connect(path)
        f = s.makefile("rwb")
        for frame in ({"v": 1, "kind": "hello", "role": "extension", "token": token},
                      {"v": 1, "kind": "request", "id": 1, "method": method, "params": params}):
            f.write(json.dumps(frame).encode() + b"\n")
        f.flush()
        for line in f:
            reply = json.loads(line)
            if reply.get("kind") == "response" and reply.get("id") == 1:
                break
        else:
            raise Failure("Le démon d'Otto a fermé la connexion")
    if not reply.get("ok"):
        raise Failure(SETUP)
    return reply["result"]


def password(acct):
    """(client secret, account password) from the item social-<name>."""
    try:
        value = daemon("secrets.get", {"name": f"social-{acct['name']}"})["value"]
    except (OSError, KeyError, TypeError, ValueError):
        raise Failure(SETUP) from None
    secret, sep, pw = str(value or "").partition(":")
    if not sep or not secret or not pw:
        raise Failure(SETUP)
    return secret, pw


# --- HTTP -------------------------------------------------------------------------

def agent(acct):
    return f"linux:otto-reddit:0.1.0 (by /u/{acct['username']})"


def token(acct):
    """A bearer token for the account, from the cache while it is valid."""
    cached = TOKENS.get(acct["name"])
    if cached and cached[1] > time.time():
        return cached[0]
    secret, pw = password(acct)
    basic = base64.b64encode(f"{acct['client_id']}:{secret}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "password", "username": acct["username"],
                                   "password": pw}).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST", headers={
        "Authorization": f"Basic {basic}", "User-Agent": agent(acct),
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            got = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            raise Failure("Reddit refuse ces identifiants. " + SETUP) from None
        raise Failure(f"Reddit a refusé la connexion ({e.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise Failure("Reddit injoignable (réseau ?)") from None
    except ValueError:
        raise Failure("Reddit a répondu quelque chose d'illisible") from None
    if not isinstance(got, dict) or not got.get("access_token"):
        # A wrong password is a 200 with {"error": "invalid_grant"}.
        raise Failure("Reddit refuse ces identifiants. " + SETUP)
    TOKENS[acct["name"]] = (got["access_token"],
                            time.time() + float(got.get("expires_in", 3600)) - 60)
    return got["access_token"]


def note_rate(headers):
    try:
        RATE["remaining"] = float(headers.get("X-Ratelimit-Remaining"))
        RATE["reset"] = time.time() + float(headers.get("X-Ratelimit-Reset"))
    except (TypeError, ValueError, AttributeError):
        pass


def api(acct, method, path, params=None, form=None):
    """One call to oauth.reddit.com, its JSON; one retry after a 429."""
    if RATE["remaining"] is not None and RATE["remaining"] < 1:
        wait = RATE["reset"] - time.time()
        if wait > 10:
            raise Failure(f"Reddit limite les requêtes: réessaie dans {int(wait)} s")
        if wait > 0:
            time.sleep(wait)
        RATE["remaining"] = None
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = urllib.parse.urlencode({**form, "api_type": "json"}).encode() if form is not None else None
    for attempt in (0, 1):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"bearer {token(acct)}", "User-Agent": agent(acct),
            "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                note_rate(r.headers)
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                try:
                    wait = float((e.headers or {}).get("Retry-After") or 1)
                except (TypeError, ValueError):
                    wait = 1.0
                time.sleep(min(wait, 5))
                continue
            if e.code == 401:
                TOKENS.pop(acct["name"], None)
                raise Failure("Reddit refuse l'accès à ce compte. " + SETUP) from None
            if e.code == 403:
                raise Failure("Reddit refuse (subreddit privé, compte banni ou action "
                              "interdite)") from None
            if e.code == 404:
                raise Failure("Introuvable sur Reddit") from None
            if e.code == 429:
                break
            raise Failure(f"Reddit a refusé la requête ({e.code})") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Failure("Reddit injoignable (réseau ?)") from None
        except ValueError:
            raise Failure("Reddit a répondu quelque chose d'illisible") from None
    raise Failure("Reddit limite les requêtes: réessaie dans un moment")


def submit(acct, path, form):
    """A POST whose answer is {"json": {"errors", "data"}}: its data, or the errors."""
    got = (api(acct, "POST", path, form=form) or {}).get("json") or {}
    if got.get("errors"):
        raise Failure("Reddit refuse: " + "; ".join(
            " ".join(str(x) for x in e[:2]) for e in got["errors"]))
    return got.get("data") or {}


# --- mapping ----------------------------------------------------------------------

def iso(ts):
    return datetime.datetime.fromtimestamp(float(ts or 0), datetime.timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def row(acct, thing):
    """A post or comment (a t3 or t1 listing child) as the front's post."""
    d = thing["data"]
    author = d.get("author") or "[deleted]"
    if thing.get("kind") == "t3":
        text = d.get("title", "")
        if d.get("selftext"):
            text += "\n\n" + d["selftext"][:EXCERPT]
    else:
        text = (d.get("body") or "")[:EXCERPT * 2]
    out = {"id": f"{acct['name']}/{d['name']}", "account": acct["name"], "network": "reddit",
           "author": author, "handle": f"u/{author}", "time": iso(d.get("created_utc")),
           "text": text, "url": SITE + (d.get("permalink") or ""),
           "likes": d.get("score", 0), "community": "r/" + d.get("subreddit", "")}
    if "num_comments" in d:
        out["replies"] = d["num_comments"]
    if thing.get("kind") == "t1" and d.get("parent_id"):
        out["reply_to"] = f"{acct['name']}/{d['parent_id']}"
    if d.get("post_hint") == "image" and d.get("url_overridden_by_dest"):
        out["media"] = [{"kind": "image", "url": d["url_overridden_by_dest"], "alt": ""}]
    return out


def listing(got):
    return [c for c in ((got or {}).get("data") or {}).get("children") or []
            if c.get("kind") in ("t1", "t3")]


def limit_of(args, top=100):
    return max(1, min(top, int(args.get("limit") or 20)))


def since_of(args):
    """The `since` bound as a POSIX time, or None."""
    s = str(args.get("since") or "").strip()
    if not s:
        return None
    try:
        t = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise Failure(f"since invalide: {s} (une date ou heure ISO)") from None
    return (t if t.tzinfo else t.astimezone()).timestamp()


def gather(accounts, fetch):
    """fetch(acct) for each account; per-account failures become `errors`, unless all failed."""
    rows, errors, first = [], [], None
    for acct in accounts:
        try:
            rows += fetch(acct)
        except Failure as e:
            first = first or e
            errors.append(f"{acct['name']}: {e}")
    if first and len(errors) == len(accounts):
        raise first
    rows.sort(key=lambda r: r["time"], reverse=True)
    return rows, errors


def parse_id(ref):
    """(account name, fullname) from `<account>/<fullname>` (`reddit:` prefix tolerated)."""
    name, _, full = str(ref or "").removeprefix("reddit:").partition("/")
    if not name or not FULLNAME.fullmatch(full):
        raise Failure(f"Identifiant Reddit invalide: {ref} (attendu <compte>/t3_… ou t1_…)")
    return name, full


# --- tools ------------------------------------------------------------------------

def accounts(args):
    return {"accounts": [{"id": a["name"], "account": a["name"], "network": "reddit",
                          "handle": f"u/{a['username']}",
                          "url": f"{SITE}/user/{a['username']}"}
                         for a in load_config().values()]}


def feed(args):
    limit, since = limit_of(args), since_of(args)

    def fetch(acct):
        got = listing(api(acct, "GET", "/best", {"limit": limit, "raw_json": 1}))
        return [row(acct, c) for c in got
                if since is None or float(c["data"].get("created_utc") or 0) > since]
    rows, errors = gather(chosen(load_config(), args.get("account")), fetch)
    return {"posts": rows[:limit], **({"errors": errors} if errors else {})}


KINDS = {"comment_reply": "reply", "post_reply": "reply", "username_mention": "mention"}


def notifications(args):
    limit, unread = limit_of(args), bool(args.get("unread"))

    def fetch(acct):
        got = api(acct, "GET", "/message/inbox", {"limit": limit, "mark": "false", "raw_json": 1})
        items = []
        for c in ((got or {}).get("data") or {}).get("children") or []:
            d = c["data"]
            if unread and not d.get("new"):
                continue
            author = d.get("author") or "reddit"
            item = {"id": f"{acct['name']}/{d['name']}", "account": acct["name"],
                    "network": "reddit", "kind": KINDS.get(d.get("type"), "other")
                    if c["kind"] == "t1" else "other", "from": author, "handle": f"u/{author}",
                    "time": iso(d.get("created_utc")), "text": (d.get("body") or "")[:EXCERPT],
                    "unread": bool(d.get("new"))}
            if c["kind"] == "t1":
                item["post_id"] = item["id"]
            if d.get("subject") and c["kind"] != "t1":
                item["text"] = f"{d['subject']}: {item['text']}"
            items.append(item)
        return items
    rows, errors = gather(chosen(load_config(), args.get("account")), fetch)
    return {"items": rows[:limit], **({"errors": errors} if errors else {})}


def search(args):
    query = str(args.get("query") or "").strip()
    if not query:
        raise Failure("Recherche vide")
    cfg, limit = load_config(), limit_of(args)
    # Public posts: one account is enough to ask (all of them would repeat the answer).
    acct = pick(cfg, args.get("account")) if args.get("account") else next(iter(cfg.values()))
    sub = SUBREDDIT.match(query)
    if sub and (len(query) == sub.end() or query[sub.end()].isspace()):
        rest = query[sub.end():].strip()
        if rest:
            got = api(acct, "GET", f"/r/{sub[1]}/search", {"q": rest, "restrict_sr": 1,
                                                           "sort": "new", "limit": limit, "raw_json": 1})
        else:
            got = api(acct, "GET", f"/r/{sub[1]}/new", {"limit": limit, "raw_json": 1})
    else:
        got = api(acct, "GET", "/search", {"q": query, "sort": "new", "limit": limit, "raw_json": 1})
    rows = sorted((row(acct, c) for c in listing(got)), key=lambda r: r["time"], reverse=True)
    return {"posts": rows}


def thread(args):
    name, full = parse_id(args.get("id"))
    acct = pick(load_config(), name)
    params, target = {"limit": 50, "depth": 2, "raw_json": 1}, None
    if full.startswith("t1_"):
        # A comment: find its post, then ask for that comment with its parents.
        info = listing(api(acct, "GET", "/api/info", {"id": full}))
        if not info:
            raise Failure("Commentaire introuvable sur Reddit")
        link = info[0]["data"]["link_id"]
        params.update(comment=full[3:], context=8)
        target = full
    else:
        link = full
    got = api(acct, "GET", f"/comments/{link[3:]}", params)
    post = row(acct, listing(got[0])[0])
    comments = listing(got[1])
    ancestors = []
    if target:
        # Walk down the chain of first children to the asked comment.
        ancestors.append(post)
        while comments and comments[0]["data"]["name"] != target:
            ancestors.append(row(acct, comments[0]))
            comments = listing(comments[0]["data"].get("replies"))
        if not comments:
            raise Failure("Commentaire introuvable sur Reddit")
        post, comments = row(acct, comments[0]), listing(comments[0]["data"].get("replies"))
    replies = []
    for c in comments:  # direct replies, each followed by its own direct replies
        replies.append(row(acct, c))
        replies += [row(acct, r) for r in listing(c["data"].get("replies"))]
    return {"post": post, "ancestors": ancestors, "replies": replies[:100]}


def post(args):
    text = str(args.get("text") or "").strip()
    if not text:
        raise Failure("Texte vide")
    cfg = load_config()
    if args.get("reply_to"):
        name, full = parse_id(args["reply_to"])
        acct = pick(cfg, name)
        data = submit(acct, "/api/comment", {"thing_id": full, "text": text})
        made = ((data.get("things") or [{}])[0]).get("data") or {}
        return {"id": f"{acct['name']}/{made.get('name', '')}",
                "url": SITE + made.get("permalink", "")}
    if args.get("account"):
        acct = pick(cfg, args["account"])
    elif len(cfg) == 1:
        acct = next(iter(cfg.values()))
    else:
        raise Failure(f"Plusieurs comptes Reddit: précise account ({', '.join(cfg)})")
    title = str(args.get("title") or "").strip()
    sub = SUBREDDIT.fullmatch(str(args.get("community") or "").strip().removeprefix("/"))
    if not title or not sub:
        raise Failure("Un nouveau post Reddit demande un titre (title) et un subreddit "
                      "(community, r/nom)")
    data = submit(acct, "/api/submit", {"kind": "self", "sr": sub[1], "title": title[:300],
                                        "text": text})
    return {"id": f"{acct['name']}/{data.get('name', '')}", "url": data.get("url", "")}


def like(args):
    name, full = parse_id(args.get("id"))
    acct = pick(load_config(), name)
    undo = bool(args.get("undo"))
    api(acct, "POST", "/api/vote", form={"id": full, "dir": 0 if undo else 1})
    return {"id": f"{acct['name']}/{full}", "liked": not undo}


# --- MCP ----------------------------------------------------------------------------

S, I, B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
ACCOUNT = {"type": "string", "description": "An account name from accounts (omitted: every account)."}
LIMIT = {"type": "integer", "description": "At most this many (default 20, 100 at most)."}
TOOLS = {
    "accounts": (accounts, {
        "description": "The Reddit accounts set up: {accounts: [{id, account, network, handle, url}]}.",
        "inputSchema": {"type": "object", "properties": {}},
    }),
    "feed": (feed, {
        "description": "The front page (best) of the subreddits the user follows, newest first.",
        "inputSchema": {"type": "object", "properties": {"account": ACCOUNT, "limit": LIMIT, "since": S}},
    }),
    "notifications": (notifications, {
        "description": "The inbox: comment replies, mentions (reply|mention) and messages (other).",
        "inputSchema": {"type": "object", "properties": {"account": ACCOUNT, "limit": LIMIT, "unread": B}},
    }),
    "search": (search, {
        "description": "Newest public posts matching words; `r/name` first searches (or lists) that subreddit.",
        "inputSchema": {"type": "object", "required": ["query"],
                        "properties": {"query": S, "account": ACCOUNT, "limit": LIMIT}},
    }),
    "thread": (thread, {
        "description": "A post (or comment) with its parents and replies.",
        "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": S}},
    }),
    "post": (post, {
        "description": "A new self post (title and community r/name needed), or a reply with reply_to.",
        "inputSchema": {"type": "object", "required": ["text"], "properties": {
            "text": S, "account": S, "reply_to": S, "title": S, "community": S}},
    }),
    "like": (like, {
        "description": "Upvote a post or comment, or clear the vote with undo: true.",
        "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": S, "undo": B}},
    }),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Réponse de Reddit inattendue ou "
                             "argument invalide"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out, "isError": False}


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None  # notification
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "otto-reddit", "version": "0.1.0"}}
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
