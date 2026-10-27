#!/usr/bin/env python3
"""MCP stdio server of the `mastodon` extension (stdlib only): the `social` provider.

Accounts in ~/.config/otto/social.toml (other providers' tables are ignored):

    [mastodon.perso]
    instance = "https://mastodon.social"
    handle = "@moi@mastodon.social"     # optional

Each account's access token is the Secret Service item social-<name>, asked of the
daemon (`secrets.get`, `[needs] secrets = ["social-*"]`): never in the file, never
logged, never in an error. Ids are `<account>/<status id>` (notifications:
`<account>/n<notification id>`); the daemon adds the `mastodon:` prefix. Failures are
`isError` results, never a crash.
"""

import datetime
import html.parser
import json
import os
import re
import socket
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = "2025-06-18"
TIMEOUT = 15
USER_AGENT = "Otto mastodon/0.2.0"
MAX_PAGE = 40
NETWORK = "mastodon"
SETUP = ("Mastodon n'est pas configuré: sur ton instance, Préférences → Développement → "
         "Nouvelle application avec les droits read et write, puis ajoute dans "
         "~/.config/otto/social.toml une table [mastodon.<nom>] avec "
         "instance = \"https://ton.instance\" (et handle si tu veux), et enregistre le "
         "jeton d'accès avec `otto provider set-key social-<nom>`.")
KINDS = {"mention": "mention", "reblog": "repost", "favourite": "like", "follow": "follow"}


class Failure(Exception):
    """A message for the model: the call failed, say why."""


# --- config and secrets --------------------------------------------------------------

def config_path():
    home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(home, "otto", "social.toml")


def load_config():
    """{name: {name, instance, handle}} from the [mastodon.<name>] tables."""
    try:
        with open(config_path(), "rb") as f:
            raw = tomllib.load(f).get("mastodon") or {}
    except FileNotFoundError:
        raise Failure(SETUP)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise Failure(f"social.toml illisible: {e}")
    out = {}
    for name, a in raw.items():
        if not isinstance(a, dict) or not re.fullmatch(r"[a-z0-9-]+", name):
            continue
        instance = str(a.get("instance", "")).rstrip("/")
        if not instance.startswith("https://"):
            continue
        out[name] = {"name": name, "instance": instance, "handle": str(a.get("handle", ""))}
    if not out:
        raise Failure(SETUP)
    return out


def daemon(method, params, timeout=10):
    """One request on the daemon socket under the `extension` role."""
    path, token = os.environ.get("OTTO_SOCKET"), os.environ.get("OTTO_EXTENSION_TOKEN")
    if not path or not token:
        raise Failure("Le démon de Otto n'est pas joignable")
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
                if not reply.get("ok"):
                    raise Failure(f"démon: {reply.get('error')}")
                return reply["result"]
    raise Failure("Le démon a fermé la connexion")


_tokens = {}


def access_token(acct):
    name = f"social-{acct['name']}"
    if name not in _tokens:
        try:
            _tokens[name] = daemon("secrets.get", {"name": name})["value"].strip()
        except (Failure, OSError, ValueError, KeyError):
            raise Failure(f"{acct['name']}: jeton {name} absent: `otto provider set-key "
                          f"{name}`. " + SETUP)
    return _tokens[name]


# --- HTTP ---------------------------------------------------------------------------

def api(acct, method, path, query=None, body=None):
    """One REST call on the account's instance, its JSON; one retry after a 429."""
    url = acct["instance"] + path + ("?" + urllib.parse.urlencode(query, doseq=True)
                                     if query else "")
    data = json.dumps(body).encode() if body is not None else None
    who = acct["name"]
    for attempt in (0, 1):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {access_token(acct)}", "User-Agent": USER_AGENT,
            "Accept": "application/json", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                try:
                    wait = float(e.headers.get("Retry-After", 1))
                except (TypeError, ValueError, AttributeError):
                    wait = 1.0
                time.sleep(min(wait, 5))
                continue
            if e.code == 401:
                _tokens.pop(f"social-{who}", None)
                raise Failure(f"{who}: l'instance refuse le jeton (révoqué ou mal copié). "
                              + SETUP)
            if e.code == 403:
                raise Failure(f"{who}: accès refusé (le jeton n'a pas les droits read et "
                              "write ?)")
            if e.code == 404:
                raise Failure(f"{who}: introuvable (supprimé, ou invisible depuis ce compte)")
            if e.code == 422:
                raise Failure(f"{who}: Mastodon a refusé: {server_error(e)}")
            raise Failure(f"{who}: Mastodon a refusé la requête ({e.code})")
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Failure(f"{who}: {acct['instance']} injoignable (réseau ?)")
        except ValueError:
            raise Failure(f"{who}: Mastodon a répondu quelque chose d'illisible")
    raise Failure(f"{who}: Mastodon limite les requêtes: réessaie dans un moment")


def server_error(e):
    try:
        return str(json.load(e).get("error") or e.reason)
    except (ValueError, AttributeError, OSError):
        return str(e.reason)


# --- mapping ------------------------------------------------------------------------

class _Text(html.parser.HTMLParser):
    """Mastodon's HTML content as plain text: paragraphs and line breaks kept."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")
        elif tag == "p" and self.parts:
            self.parts.append("\n\n")

    def handle_data(self, data):
        self.parts.append(data)


def plain(content):
    p = _Text()
    p.feed(content or "")
    p.close()
    return re.sub(r"\n{3,}", "\n\n", "".join(p.parts)).strip()


def handle_of(acct, who):
    """@user@host, also for local users (whose `acct` has no host)."""
    name = who.get("acct") or who.get("username", "")
    if "@" not in name:
        name += "@" + urllib.parse.urlsplit(acct["instance"]).hostname
    return "@" + name


def media(a):
    kind = a.get("type") if a.get("type") in ("image", "video", "audio") else (
        "video" if a.get("type") == "gifv" else "other")
    return {"kind": kind, "url": a.get("url") or a.get("remote_url") or "",
            "alt": a.get("description") or ""}


def post_of(acct, s):
    """A status as a social post; a reblog shows the original, with who boosted it."""
    booster = None
    if s.get("reblog"):
        booster, s = s.get("account") or {}, s["reblog"]
    who = s.get("account") or {}
    text = plain(s.get("content"))
    if s.get("spoiler_text"):
        text = f"[CW: {s['spoiler_text']}] {text}"
    out = {"id": f"{acct['name']}/{s['id']}", "account": acct["name"], "network": NETWORK,
           "author": who.get("display_name") or who.get("username", ""),
           "handle": handle_of(acct, who), "time": s.get("created_at", ""), "text": text,
           "url": s.get("url") or s.get("uri") or "",
           "replies": s.get("replies_count", 0), "likes": s.get("favourites_count", 0),
           "reposts": s.get("reblogs_count", 0)}
    if s.get("media_attachments"):
        out["media"] = [media(a) for a in s["media_attachments"]]
    if s.get("in_reply_to_id"):
        out["reply_to"] = f"{acct['name']}/{s['in_reply_to_id']}"
    if s.get("favourited"):
        out["liked"] = True
    if booster is not None:
        out["reposted_by"] = booster.get("display_name") or booster.get("username", "")
        out["reposted_handle"] = handle_of(acct, booster)
    return out


def when(value):
    """An ISO date or time as an aware datetime (naive: local time)."""
    t = datetime.datetime.fromisoformat(value.strip())
    return t if t.tzinfo else t.astimezone()


# --- accounts and ids -----------------------------------------------------------------

def chosen(args):
    """The accounts a call is about: the `account` one, or every one."""
    cfg = load_config()
    name = (args.get("account") or "").strip()
    if not name:
        return list(cfg.values())
    if name not in cfg:
        raise Failure(f"Pas de compte Mastodon {name!r}; comptes: {', '.join(cfg)}")
    return [cfg[name]]


def gather(args, fn, key):
    """fn(account) for each chosen account, rows merged; with several accounts a failing
    one becomes a line of `errors` and the others still answer."""
    accts = chosen(args)
    if len(accts) == 1:
        return {key: fn(accts[0])}
    rows, errors = [], []
    for a in accts:
        try:
            rows += fn(a)
        except Failure as e:
            errors.append(str(e))
    return {key: rows, **({"errors": errors} if errors else {})}


def status_ref(value):
    """(account, status id) of a `<account>/<status id>` id."""
    name, _, sid = str(value or "").strip().partition("/")
    if not re.fullmatch(r"[A-Za-z0-9]+", sid):  # numeric on Mastodon, flakes on Pleroma
        raise Failure(f"Identifiant de post Mastodon invalide: {value!r} (attendu: compte/numéro)")
    cfg = load_config()
    if name not in cfg:
        raise Failure(f"Pas de compte Mastodon {name!r}; comptes: {', '.join(cfg)}")
    return cfg[name], sid


def limit(args):
    return max(1, min(MAX_PAGE, int(args.get("limit") or 20)))


# --- tools --------------------------------------------------------------------------

def accounts(args):
    def one(a):
        me = api(a, "GET", "/api/v1/accounts/verify_credentials")
        return [{"id": a["name"], "account": a["name"], "network": NETWORK,
                 "handle": a["handle"] or handle_of(a, me), "url": me.get("url", "")}]
    return gather(args, one, "accounts")


def feed(args):
    n = limit(args)
    since = when(args["since"]) if args.get("since") else None

    def one(a):
        rows = api(a, "GET", "/api/v1/timelines/home", {"limit": n}) or []
        # ponytail: one page of 40 at most; page with max_id if `since` must reach further.
        if since:
            rows = [s for s in rows if when(s["created_at"]) > since]
        return [post_of(a, s) for s in rows]
    return gather(args, one, "posts")


def notifications(args):
    n = limit(args)

    def one(a):
        rows = api(a, "GET", "/api/v1/notifications", {"limit": n}) or []
        if args.get("unread"):
            markers = api(a, "GET", "/api/v1/markers", {"timeline[]": "notifications"}) or {}
            seen = int((markers.get("notifications") or {}).get("last_read_id") or 0)
            rows = [r for r in rows if int(r["id"]) > seen]
        out = []
        for r in rows:
            who, s = r.get("account") or {}, r.get("status")
            kind = KINDS.get(r.get("type"), "other")
            if kind == "mention" and s and s.get("visibility") == "direct":
                kind = "message"
            elif kind == "mention" and s and s.get("in_reply_to_id"):
                kind = "reply"
            item = {"id": f"{a['name']}/n{r['id']}", "account": a["name"], "network": NETWORK,
                    "kind": kind, "from": who.get("display_name") or who.get("username", ""),
                    "handle": handle_of(a, who), "time": r.get("created_at", ""),
                    "url": (s or {}).get("url") or who.get("url", "")}
            if s:
                item["text"] = plain(s.get("content"))
                item["post_id"] = f"{a['name']}/{s['id']}"
            out.append(item)
        return out
    return gather(args, one, "items")


def search(args):
    q = (args.get("query") or "").strip()
    if not q:
        raise Failure("Recherche vide")
    n = limit(args)
    tag = re.fullmatch(r"#(\w+)", q)

    def one(a):
        if tag:
            rows = api(a, "GET", f"/api/v1/timelines/tag/{urllib.parse.quote(tag[1])}",
                       {"limit": n}) or []
        else:
            rows = (api(a, "GET", "/api/v2/search", {"q": q, "type": "statuses", "limit": n})
                    or {}).get("statuses") or []
        return [post_of(a, s) for s in rows]
    return gather(args, one, "posts")


def thread(args):
    a, sid = status_ref(args.get("id"))
    post = api(a, "GET", f"/api/v1/statuses/{sid}")
    ctx = api(a, "GET", f"/api/v1/statuses/{sid}/context") or {}
    return {"post": post_of(a, post),
            "ancestors": [post_of(a, s) for s in ctx.get("ancestors") or []],
            "replies": [post_of(a, s) for s in ctx.get("descendants") or []]}


def post(args):
    text = (args.get("text") or "").strip()
    if not text:
        raise Failure("Post vide")
    body = {"status": text}
    if args.get("reply_to"):
        a, sid = status_ref(args["reply_to"])
        body["in_reply_to_id"] = sid
    else:
        accts = chosen(args)
        if len(accts) > 1:
            raise Failure("Plusieurs comptes Mastodon: lequel publie ? (account: "
                          + ", ".join(x["name"] for x in accts) + ")")
        a = accts[0]
    # The instance's character limit (500 by default) is its own: a 422 says it.
    s = api(a, "POST", "/api/v1/statuses", body=body)
    return {"id": f"{a['name']}/{s['id']}", "url": s.get("url") or s.get("uri") or ""}


def like(args):
    a, sid = status_ref(args.get("id"))
    undo = bool(args.get("undo"))
    s = api(a, "POST", f"/api/v1/statuses/{sid}/{'unfavourite' if undo else 'favourite'}")
    return {"id": f"{a['name']}/{sid}", "liked": bool((s or {}).get("favourited", not undo))}


# --- social.notification events -------------------------------------------------------

POLL_SECONDS = 300
SIGNAL_KINDS = ("mention", "reply", "message", "follow")  # not likes nor reposts
MAX_EVENTS = 10   # per account and check, newest kept
KEEP = 300        # notification ids remembered per account
EXCERPT_EVENT = 200


def state_path():
    # The path the sandbox grants (`[needs] write`); MASTODON_STATE for the tests.
    return os.environ.get("MASTODON_STATE") or os.path.expanduser(
        "~/.local/state/otto/mastodon/notifications.json")


def load_state():
    try:
        with open(state_path()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(data):
    path = state_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def check_notifications(publish):
    """Publishes `social.notification` for each new mention, reply, message or follow,
    oldest first. An account's first check only remembers what is there: no old news."""
    state = load_state()
    for name in load_config():
        try:
            items = notifications({"account": name, "limit": 40})["items"]
        except Failure:
            continue
        known = state.get(name)
        if known is not None:
            seen = set(known)
            new = [i for i in items if i["id"] not in seen and i["kind"] in SIGNAL_KINDS]
            for i in reversed(new[:MAX_EVENTS]):
                publish({"account": i["account"], "network": i["network"], "kind": i["kind"],
                         "from": i["from"], "handle": i["handle"],
                         "text": (i.get("text") or "")[:EXCERPT_EVENT], "url": i.get("url", ""),
                         "id": i["id"], "date": i["time"]})
        ids = [i["id"] for i in items]
        state[name] = (ids + [k for k in known or [] if k not in ids])[:KEEP]
    save_state(state)


def publish_event(data):
    try:
        daemon("events.publish", {"type": "social.notification", "data": data}, timeout=5)
    except (Failure, OSError, ValueError):
        pass


def watch_forever():
    while True:
        try:
            check_notifications(publish_event)
        except Exception as e:  # not set up, network: try again next round, quietly
            if not isinstance(e, Failure):
                print(f"mastodon watch: {type(e).__name__}", file=sys.stderr, flush=True)
        time.sleep(POLL_SECONDS)


# --- MCP ---------------------------------------------------------------------------

S, I, B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
ACCOUNT = {"type": "string", "description": "An account name (omitted: every account)."}
LIMIT = {"type": "integer", "description": "At most this many (default 20, 40 at most)."}
POST_ID = {"type": "string", "description": "A post id: <account>/<status id>."}
TOOLS = {
    "accounts": (accounts, {
        "description": "The Mastodon accounts: {accounts: [{id, account, network, handle, url}]}.",
        "inputSchema": {"type": "object", "properties": {}},
    }),
    "feed": (feed, {
        "description": "The home timeline, newest first; reblogs show the original post.",
        "inputSchema": {"type": "object", "properties": {
            "account": ACCOUNT, "limit": LIMIT,
            "since": {"type": "string", "description": "Only posts after this ISO date or time."}}},
    }),
    "notifications": (notifications, {
        "description": "Mentions, replies, likes, reposts, follows, newest first.",
        "inputSchema": {"type": "object", "properties": {
            "account": ACCOUNT, "limit": LIMIT, "unread": B}},
    }),
    "search": (search, {
        "description": "Posts matching words, or a #tag's timeline.",
        "inputSchema": {"type": "object", "required": ["query"], "properties": {
            "query": S, "account": ACCOUNT, "limit": LIMIT}},
    }),
    "thread": (thread, {
        "description": "A post with its ancestors and replies.",
        "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": POST_ID}},
    }),
    "post": (post, {
        "description": "Publishes a post (the account's default visibility), or a reply with "
                       "reply_to: {id, url}.",
        "inputSchema": {"type": "object", "required": ["text"], "properties": {
            "text": S, "account": ACCOUNT, "reply_to": POST_ID, "title": S, "community": S}},
    }),
    "like": (like, {
        "description": "Favourites a post, or takes it back with undo: true: {id, liked}.",
        "inputSchema": {"type": "object", "required": ["id"], "properties": {
            "id": POST_ID, "undo": B}},
    }),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Réponse de Mastodon inattendue ou "
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
                  "serverInfo": {"name": "otto-mastodon", "version": "0.2.0"}}
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
    if os.environ.get("OTTO_SOCKET"):
        threading.Thread(target=watch_forever, daemon=True).start()
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
