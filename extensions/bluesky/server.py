#!/usr/bin/env python3
"""MCP stdio server of the `bluesky` extension (stdlib only).

A provider of Otto's `social` front on the AT Protocol (XRPC over HTTPS). Accounts in
~/.config/otto/social.toml (SOCIAL_CONFIG overrides, for tests):

    [bluesky.perso]
    handle = "name.bsky.social"
    service = "https://bsky.social"     # the PDS (optional)

Each account's APP password is the Secret Service item social-<name>, asked of the
daemon (`secrets.get`, `[needs] secrets = ["social-*"]`) when a session opens; the
session (access and refresh tokens) is kept for the process. Other providers' tables
are ignored. Post ids are `<account>|<at:// uri>|<cid>` (the cid may be empty, e.g.
for a notification's subject: it is then looked up). Methods: accounts, feed,
notifications, search, thread, post, like. Nothing is logged; no error carries a secret.
"""

import datetime
import json
import os
import re
import socket
import sys
import threading
import time
import tomllib
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = "2025-06-18"
SERVICE = "https://bsky.social"
TIMEOUT = 15
AGENT = "Otto/0.1 (https://github.com/leur-ia/samantha-extensions; bluesky provider)"
MAX_POST = 300  # graphemes
NAME = re.compile(r"^[a-z0-9-]+$")
KINDS = {"like": "like", "repost": "repost", "follow": "follow", "mention": "mention", "reply": "reply"}
UTC = datetime.timezone.utc


class Failure(Exception):
    """A message for the model: the call failed, say why."""


class Expired(Failure):
    """The access token expired: refresh the session and try again."""


def setup(name="<nom>"):
    return (f"Bluesky n'est pas configuré pour le compte {name}: ajoute-le dans "
            f"~/.config/otto/social.toml ([bluesky.{name}] avec handle = \"{name}.bsky.social\", "
            "et service = \"https://…\" si ton serveur n'est pas bsky.social), crée un mot de "
            "passe d'application dans Bluesky (Paramètres → Confidentialité et sécurité → Mots "
            "de passe d'application; jamais ton mot de passe principal) et enregistre-le avec "
            f"`otto provider set-key social-{name}`.")


# --- config and secrets --------------------------------------------------------------

def config_path():
    if os.environ.get("SOCIAL_CONFIG"):
        return os.environ["SOCIAL_CONFIG"]
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "otto", "social.toml")


def load_config():
    """{name: {name, handle, service}} from the [bluesky.<name>] tables."""
    try:
        with open(config_path(), "rb") as f:
            raw = tomllib.load(f).get("bluesky") or {}
    except FileNotFoundError:
        raw = {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise Failure(f"social.toml illisible: {e}")
    out = {}
    for name, a in raw.items():
        if not NAME.match(name) or not isinstance(a, dict) or not a.get("handle"):
            continue
        service = str(a.get("service") or SERVICE).rstrip("/")
        if not service.startswith("https://"):
            continue
        out[name] = {"name": name, "handle": str(a["handle"]).strip().lstrip("@"), "service": service}
    if not out:
        raise Failure(setup())
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


def password(acct):
    try:
        return daemon("secrets.get", {"name": f"social-{acct['name']}"})["value"]
    except (Failure, OSError, KeyError, TypeError):
        raise Failure(setup(acct["name"]))


def plain(s):
    """An id or account name without the daemon's `bluesky:` prefix."""
    return str(s or "").strip().removeprefix("bluesky:")


def pick(cfg, name):
    name = plain(name)
    if name not in cfg:
        raise Failure(f"Pas de compte Bluesky {name!r}; comptes: {', '.join(cfg)}")
    return cfg[name]


def chosen(cfg, args):
    """The account asked for, or every account."""
    return [pick(cfg, args["account"])] if args.get("account") else list(cfg.values())


# --- XRPC ----------------------------------------------------------------------------

def http(acct, nsid, params=None, body=None, token=None, post=False):
    """One XRPC call (GET with params, POST with body or `post`), its JSON; one retry after a 429."""
    url = f"{acct['service']}/xrpc/{nsid}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": AGENT, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in (0, 1):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST" if post or data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(1)
                continue
            try:
                error = str(json.load(e).get("error") or "")
            except (ValueError, AttributeError, OSError):
                error = ""
            if error == "ExpiredToken":
                raise Expired("Session Bluesky expirée")
            if e.code == 401:
                raise Failure(f"Bluesky refuse la connexion de {acct['handle']} (mot de passe "
                              "d'application révoqué ou faux ?). " + setup(acct["name"]))
            if e.code == 429:
                raise Failure("Bluesky limite les requêtes: réessaie dans un moment")
            if e.code == 404 or error in ("NotFound", "RecordNotFound"):
                raise Failure("Introuvable sur Bluesky (supprimé ou masqué ?)")
            raise Failure(f"Bluesky a refusé la requête ({e.code}{': ' + error if error else ''})")
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Failure("Bluesky injoignable (réseau ?)")
        except ValueError:
            raise Failure("Bluesky a répondu quelque chose d'illisible")
    raise Failure("Bluesky limite les requêtes: réessaie dans un moment")


_sessions = {}


def key(acct):
    return (acct["name"], acct["handle"], acct["service"])


def session(acct):
    """{accessJwt, refreshJwt, did, handle}, opened once per process."""
    if key(acct) not in _sessions:
        s = http(acct, "com.atproto.server.createSession",
                 body={"identifier": acct["handle"], "password": password(acct)})
        _sessions[key(acct)] = {k: s[k] for k in ("accessJwt", "refreshJwt", "did", "handle")}
    return _sessions[key(acct)]


def renew(acct):
    """A refreshed session, or none (the next call opens a new one)."""
    old = _sessions.pop(key(acct), None)
    if not old:
        return
    try:
        s = http(acct, "com.atproto.server.refreshSession", token=old["refreshJwt"], post=True)
    except Failure:
        return
    _sessions[key(acct)] = {k: s.get(k) or old[k] for k in ("accessJwt", "refreshJwt", "did", "handle")}


def xrpc(acct, nsid, params=None, body=None):
    """An authenticated call; an expired token is refreshed (or the session reopened) once."""
    for last in (False, True):
        try:
            return http(acct, nsid, params, body, session(acct)["accessJwt"])
        except Expired:
            if last:
                raise Failure("Session Bluesky expirée: réessaie")
            renew(acct)


# --- posts ---------------------------------------------------------------------------

def make_id(acct, uri, cid=""):
    return f"{acct['name']}|{uri}|{cid or ''}"


def web_url(uri, handle=""):
    """bsky.app's page of a post's at:// uri; else the profile of `handle`."""
    m = re.fullmatch(r"at://([^/]+)/app\.bsky\.feed\.post/([^/]+)", uri)
    if m:
        return f"https://bsky.app/profile/{m[1]}/post/{m[2]}"
    return f"https://bsky.app/profile/{handle}" if handle else ""


def parse_id(cfg, pid):
    name, _, rest = plain(pid).partition("|")
    uri, _, cid = rest.partition("|")
    if not uri.startswith("at://"):
        raise Failure("id invalide: un id de social.feed, search, thread ou notifications")
    return pick(cfg, name), uri, cid


def media(embed):
    embed = embed or {}
    kind = embed.get("$type", "")
    if kind.startswith("app.bsky.embed.recordWithMedia"):
        return media(embed.get("media"))
    if kind.startswith("app.bsky.embed.images"):
        return [{"kind": "image", "url": i.get("fullsize", ""), "alt": i.get("alt", "")}
                for i in embed.get("images") or []]
    if kind.startswith("app.bsky.embed.video"):
        return [{"kind": "video", "url": embed.get("playlist", ""), "alt": embed.get("alt", "")}]
    return []


def post_of(acct, view, reason=None):
    rec = view.get("record") or {}
    author = view.get("author") or {}
    handle = author.get("handle", "")
    out = {"id": make_id(acct, view["uri"], view.get("cid")), "account": acct["name"], "network": "bluesky",
           "author": author.get("displayName") or handle, "handle": handle,
           "time": view.get("indexedAt") or rec.get("createdAt", ""), "text": rec.get("text", ""),
           "url": f"https://bsky.app/profile/{handle}/post/{view['uri'].rsplit('/', 1)[-1]}"}
    for ours, theirs in (("replies", "replyCount"), ("likes", "likeCount"), ("reposts", "repostCount")):
        if view.get(theirs) is not None:
            out[ours] = view[theirs]
    found = media(view.get("embed"))
    if found:
        out["media"] = found
    parent = (rec.get("reply") or {}).get("parent")
    if parent and parent.get("uri"):
        out["reply_to"] = make_id(acct, parent["uri"], parent.get("cid"))
    if reason and reason.get("$type", "").startswith("app.bsky.feed.defs#reasonRepost"):
        by = reason.get("by") or {}
        out["reposted_by"] = by.get("displayName") or by.get("handle", "")
        out["time"] = reason.get("indexedAt") or out["time"]
    return out


def when(s):
    t = datetime.datetime.fromisoformat(str(s).strip())
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def limit_of(args):
    return max(1, min(100, int(args.get("limit") or 20)))


def gather(cfg, args, one):
    """one(acct) for each chosen account: merged rows newest first, failures in errors."""
    rows, errors = [], []
    accts = chosen(cfg, args)
    for acct in accts:
        try:
            rows += one(acct)
        except Failure as e:
            errors.append(f"{acct['name']}: {e}")
    if errors and not rows and len(errors) == len(accts):
        raise Failure(" ; ".join(errors))
    rows.sort(key=lambda r: r["time"], reverse=True)
    return rows, errors


def get_post(acct, uri):
    found = xrpc(acct, "app.bsky.feed.getPosts", {"uris": [uri]}).get("posts") or []
    if not found:
        raise Failure("Post introuvable sur Bluesky (supprimé ou masqué ?)")
    return found[0]


# --- tools ---------------------------------------------------------------------------

def accounts(args):
    cfg = load_config()

    def one(acct):
        p = xrpc(acct, "app.bsky.actor.getProfile", {"actor": acct["handle"]})
        handle = p.get("handle") or acct["handle"]
        return [{"id": acct["name"], "account": acct["name"], "network": "bluesky", "handle": handle,
                 "name": p.get("displayName", ""), "url": f"https://bsky.app/profile/{handle}", "time": ""}]
    rows, errors = gather(cfg, {}, one)
    for r in rows:
        del r["time"]
    rows.sort(key=lambda r: r["account"])
    return {"accounts": rows, **({"errors": errors} if errors else {})}


def feed(args):
    cfg, limit = load_config(), limit_of(args)
    since = when(args["since"]) if args.get("since") else None

    def one(acct):
        # ponytail: one page only (≤ 100 posts); paginate with `cursor` if since needs more.
        got = xrpc(acct, "app.bsky.feed.getTimeline", {"limit": limit}).get("feed") or []
        posts = [post_of(acct, f["post"], f.get("reason")) for f in got]
        return [p for p in posts if not since or when(p["time"]) > since]
    rows, errors = gather(cfg, args, one)
    return {"posts": rows[:limit], **({"errors": errors} if errors else {})}


def notifications(args):
    cfg, limit = load_config(), limit_of(args)

    def one(acct):
        got = xrpc(acct, "app.bsky.notification.listNotifications", {"limit": limit}).get("notifications") or []
        items = []
        for n in got:
            if args.get("unread") and n.get("isRead"):
                continue
            author, rec = n.get("author") or {}, n.get("record") or {}
            item = {"id": make_id(acct, n["uri"], n.get("cid")), "account": acct["name"], "network": "bluesky",
                    "kind": KINDS.get(n.get("reason"), "other"), "from": author.get("displayName") or author.get("handle", ""),
                    "handle": author.get("handle", ""), "time": n.get("indexedAt", ""),
                    "url": web_url(n["uri"] if n.get("reason") in ("mention", "reply", "quote")
                                   else n.get("reasonSubject") or "", author.get("handle", ""))}
            if rec.get("text"):
                item["text"] = rec["text"]
            if n.get("reason") in ("mention", "reply", "quote"):
                item["post_id"] = item["id"]  # the notification is the post
            elif n.get("reasonSubject", "").startswith("at://"):
                item["post_id"] = make_id(acct, n["reasonSubject"])  # the user's post, cid looked up later
            items.append(item)
        return items
    rows, errors = gather(cfg, args, one)
    return {"items": rows[:limit], **({"errors": errors} if errors else {})}


def search(args):
    query = str(args.get("query") or "").strip()
    if not query:
        raise Failure("Que chercher ? (query)")
    cfg, limit = load_config(), limit_of(args)
    # One account is enough to search the public network: the same results otherwise.
    acct = pick(cfg, args["account"]) if args.get("account") else next(iter(cfg.values()))
    got = xrpc(acct, "app.bsky.feed.searchPosts", {"q": query, "sort": "latest", "limit": limit}).get("posts") or []
    return {"posts": [post_of(acct, p) for p in got][:limit]}


def thread(args):
    acct, uri, _ = parse_id(load_config(), args.get("id"))
    t = xrpc(acct, "app.bsky.feed.getPostThread", {"uri": uri, "depth": 3, "parentHeight": 20}).get("thread") or {}
    if "post" not in t:
        raise Failure("Post introuvable sur Bluesky (supprimé, bloqué ou masqué ?)")
    ancestors, node = [], t.get("parent")
    while node and "post" in node:
        ancestors.append(post_of(acct, node["post"]))
        node = node.get("parent")
    replies = []

    def walk(nodes):
        for n in nodes or []:
            if "post" in n:
                replies.append(post_of(acct, n["post"]))
                walk(n.get("replies"))
    walk(t.get("replies"))
    return {"post": post_of(acct, t["post"]), "ancestors": ancestors[::-1], "replies": replies}


def graphemes(s):
    """User-perceived characters, close to UAX #29: marks, variation selectors, skin
    tones and tags extend the previous one; ZWJ joins; regional indicators pair."""
    # ponytail: approximates UAX #29 (no Hangul jamo or Indic conjunct rules); exact enough for a 300 limit.
    n, join, flag = 0, False, False
    for ch in s:
        o = ord(ch)
        if o == 0x200D:
            join = True
            continue
        if unicodedata.category(ch).startswith("M") or 0xFE00 <= o <= 0xFE0F \
                or 0x1F3FB <= o <= 0x1F3FF or 0xE0020 <= o <= 0xE007F:
            continue
        pair = 0x1F1E6 <= o <= 0x1F1FF and flag
        flag = 0x1F1E6 <= o <= 0x1F1FF and not pair
        if join or pair:
            join = False
            continue
        n += 1
    return n


def stamp():
    return datetime.datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def post(args):
    text = str(args.get("text") or "").strip()
    if not text:
        raise Failure("Post vide")
    count = graphemes(text)
    if count > MAX_POST:
        raise Failure(f"Post trop long pour Bluesky ({count} caractères, {MAX_POST} au plus)")
    cfg = load_config()
    record = {"$type": "app.bsky.feed.post", "text": text, "createdAt": stamp()}
    if args.get("reply_to"):
        acct, uri, _ = parse_id(cfg, args["reply_to"])
        parent = get_post(acct, uri)
        ref = {"uri": parent["uri"], "cid": parent["cid"]}
        root = ((parent.get("record") or {}).get("reply") or {}).get("root") or ref
        record["reply"] = {"root": {"uri": root["uri"], "cid": root["cid"]}, "parent": ref}
    elif args.get("account"):
        acct = pick(cfg, args["account"])
    elif len(cfg) == 1:
        acct = next(iter(cfg.values()))
    else:
        raise Failure(f"Plusieurs comptes Bluesky: précise account ({', '.join(cfg)})")
    s = session(acct)
    made = xrpc(acct, "com.atproto.repo.createRecord",
                body={"repo": s["did"], "collection": "app.bsky.feed.post", "record": record})
    return {"id": make_id(acct, made["uri"], made.get("cid")),
            "url": f"https://bsky.app/profile/{s['handle']}/post/{made['uri'].rsplit('/', 1)[-1]}"}


def like(args):
    acct, uri, cid = parse_id(load_config(), args.get("id"))
    did = session(acct)["did"]
    if args.get("undo"):
        mine = (get_post(acct, uri).get("viewer") or {}).get("like")
        if mine:
            xrpc(acct, "com.atproto.repo.deleteRecord",
                 body={"repo": did, "collection": "app.bsky.feed.like", "rkey": mine.rsplit("/", 1)[-1]})
        return {"id": make_id(acct, uri, cid), "liked": False}
    if not cid:
        cid = get_post(acct, uri)["cid"]
    xrpc(acct, "com.atproto.repo.createRecord", body={
        "repo": did, "collection": "app.bsky.feed.like",
        "record": {"$type": "app.bsky.feed.like", "subject": {"uri": uri, "cid": cid}, "createdAt": stamp()}})
    return {"id": make_id(acct, uri, cid), "liked": True}


# --- social.notification events -------------------------------------------------------

POLL_SECONDS = 300
SIGNAL_KINDS = ("mention", "reply", "message", "follow")  # not likes nor reposts
MAX_EVENTS = 10   # per account and check, newest kept
KEEP = 300        # notification ids remembered per account
EXCERPT_EVENT = 200


def state_path():
    # The path the sandbox grants (`[needs] write`); BLUESKY_STATE for the tests.
    return os.environ.get("BLUESKY_STATE") or os.path.expanduser(
        "~/.local/state/otto/bluesky/notifications.json")


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
                print(f"bluesky watch: {type(e).__name__}", file=sys.stderr, flush=True)
        time.sleep(POLL_SECONDS)


# --- MCP -----------------------------------------------------------------------------

S, I, B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
ACCOUNT = {"type": "string", "description": "An account name from accounts (omitted: every account)."}
LIMIT = {"type": "integer", "description": "At most this many (default 20, 100 at most)."}
POST = "{id, account, network, author, handle, time, text, url, replies?, likes?, reposts?, media?, reply_to?, reposted_by?}"
TOOLS = {
    "accounts": (accounts, "The Bluesky accounts: {accounts: [{id, account, network, handle, name, url}]}.",
                 {"type": "object", "properties": {}}),
    "feed": (feed, f"The home timeline, newest first: {{posts: [{POST}], errors?}}.",
             {"type": "object", "properties": {"account": ACCOUNT, "limit": LIMIT, "since": S}}),
    "notifications": (notifications, "Mentions, replies, likes, reposts, follows, newest first: "
                      "{items: [{id, account, network, kind, from, handle, time, text?, post_id?}], errors?}.",
                      {"type": "object", "properties": {"account": ACCOUNT, "limit": LIMIT, "unread": B}}),
    "search": (search, f"Public posts matching words or a #tag, newest first: {{posts: [{POST}]}}.",
               {"type": "object", "required": ["query"], "properties": {"query": S, "account": ACCOUNT, "limit": LIMIT}}),
    "thread": (thread, "A post with what it answers (ancestors, oldest first) and its replies.",
               {"type": "object", "required": ["id"], "properties": {"id": S}}),
    "post": (post, "Publishes a post (300 characters at most), or a reply with reply_to: {id, url}.",
             {"type": "object", "required": ["text"], "properties": {
                 "text": S, "account": S, "reply_to": S, "title": S, "community": S}}),
    "like": (like, "Likes a post, or takes the like back with undo: {id, liked}.",
             {"type": "object", "required": ["id"], "properties": {"id": S, "undo": B}}),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Réponse de Bluesky inattendue ou argument invalide"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out, "isError": False}


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None  # notification
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "otto-bluesky", "version": "0.2.0"}}
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
    if os.environ.get("OTTO_SOCKET"):
        threading.Thread(target=watch_forever, daemon=True).start()
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
