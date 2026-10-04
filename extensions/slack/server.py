#!/usr/bin/env python3
"""MCP stdio server of the `slack` extension (stdlib only).

The official Slack Web API (https://slack.com/api) with the token of the user's own
internal Slack app, SLACK_TOKEN in the environment (from the Secret Service). A user
token (xoxp-…) sees what the user sees and can search; a bot token (xoxb-…) sees the
channels the bot was added to. Browser session tokens (xoxc/xoxd) are refused: they
aren't an official way in. Failures are `isError` results, never a crash; nothing is
logged and no error carries the token.
"""

import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = "2025-06-18"
API = "https://slack.com/api/"
TIMEOUT = 10
MAX_TEXT = 4000      # characters of one message returned to the model
SETUP = ("Slack n'est pas configuré: crée une app interne sur api.slack.com/apps, ajoute "
         "les scopes utilisateur channels:read groups:read im:read mpim:read "
         "channels:history groups:history im:history mpim:history users:read search:read "
         "chat:write reactions:write, installe-la dans ton espace de travail, puis "
         "enregistre le User OAuth Token (xoxp-…) avec `samantha provider set-key slack`.")
CHANNEL_ID = re.compile(r"^[CGD][A-Z0-9]{6,}$")
TS = re.compile(r"^\d{9,}\.\d{1,9}$")


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def token():
    tok = os.environ.get("SLACK_TOKEN", "").strip()
    if not tok:
        raise Failure(SETUP)
    if tok.startswith(("xoxc-", "xoxd-")):
        raise Failure("Ce jeton est un jeton de session du navigateur (xoxc/xoxd): ce n'est "
                      "pas un accès officiel. " + SETUP)
    return tok


def api(method, **params):
    """One Web API call (form POST), its JSON when `ok`; one retry after a 429."""
    body = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    for attempt in (0, 1):
        req = urllib.request.Request(API + method, data=body, method="POST", headers={
            "Authorization": f"Bearer {token()}",
            "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(min(float(e.headers.get("Retry-After") or 1), 5))
                continue
            raise Failure(f"Slack a refusé la requête ({e.code})")
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Failure("Slack injoignable (réseau ?)")
        except ValueError:
            raise Failure("Slack a répondu quelque chose d'illisible")
    if not data.get("ok"):
        raise Failure(explain(data))
    return data


def explain(data):
    err = data.get("error", "erreur inconnue")
    if err == "missing_scope":
        return (f"Il manque le scope {data.get('needed', '?')} à l'app Slack: ajoute-le sur "
                "api.slack.com/apps, réinstalle l'app et réenregistre le jeton.")
    if err == "not_allowed_token_type":
        return "Cette action demande un jeton utilisateur (xoxp-…), pas un jeton de bot."
    if err in ("invalid_auth", "not_authed", "token_revoked", "account_inactive"):
        return "Slack refuse le jeton: " + SETUP
    if err == "channel_not_found":
        return "Canal introuvable (ou l'app n'y a pas accès)."
    if err == "not_in_channel":
        return "L'app n'est pas membre de ce canal: invite-la ou utilise un jeton utilisateur."
    return f"Slack: {err}"


def when(ts):
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError):
        return ""


_users = {}     # user id -> display name
_channels = {}  # lowercased name -> id


def user_name(uid):
    if not uid:
        return ""
    if uid not in _users:
        try:
            u = api("users.info", user=uid)["user"]
            p = u.get("profile") or {}
            _users[uid] = p.get("display_name") or p.get("real_name") or u.get("name") or uid
        except Failure:
            _users[uid] = uid
    return _users[uid]


def conversations(types, limit):
    """Every conversation of `types` the token sees, up to `limit`, following cursors."""
    out, cursor = [], None
    while len(out) < limit:
        data = api("conversations.list", types=types, exclude_archived="true",
                   limit=min(200, limit - len(out)), cursor=cursor)
        out += data.get("channels") or []
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return out[:limit]


def channel_id(ref):
    ref = (ref or "").strip()
    if not ref:
        raise Failure("Donne un canal (#nom ou identifiant)")
    if CHANNEL_ID.match(ref):
        return ref
    name = ref.lstrip("#").lower()
    if name not in _channels:
        for c in conversations("public_channel,private_channel", 1000):
            if c.get("name"):
                _channels[c["name"].lower()] = c["id"]
    if name not in _channels:
        raise Failure(f"Canal introuvable: #{name}")
    return _channels[name]


def message(m):
    return {"ts": m.get("ts", ""), "time": when(m.get("ts")),
            "user": user_name(m.get("user")) or m.get("username") or "",
            "text": (m.get("text") or "")[:MAX_TEXT],
            "replies": m.get("reply_count", 0)}


def channels(args):
    limit = max(1, min(500, int(args.get("limit") or 100)))
    rows = []
    for c in conversations("public_channel,private_channel,mpim,im", limit):
        if c.get("is_im"):
            kind, name = "dm", user_name(c.get("user"))
        else:
            kind = "private" if c.get("is_private") or c.get("is_mpim") else "public"
            name = c.get("name") or ""
        rows.append({"id": c["id"], "name": name, "kind": kind,
                     "member": bool(c.get("is_member", c.get("is_im")))})
    return {"channels": rows}


def history(args):
    cid = channel_id(args.get("channel"))
    limit = max(1, min(50, int(args.get("limit") or 20)))
    data = api("conversations.history", channel=cid, limit=limit, oldest=args.get("oldest"))
    # Oldest first, the order a conversation is read in.
    return {"channel": cid, "messages": [message(m) for m in reversed(data.get("messages") or [])]}


def thread(args):
    cid, ts = channel_id(args.get("channel")), args.get("ts", "")
    if not TS.match(str(ts)):
        raise Failure("ts invalide: prends le ts d'un message renvoyé par slack.history")
    data = api("conversations.replies", channel=cid, ts=ts, limit=100)
    return {"channel": cid, "messages": [message(m) for m in data.get("messages") or []]}


def search(args):
    query = (args.get("query") or "").strip()
    if not query:
        raise Failure("Recherche vide")
    limit = max(1, min(20, int(args.get("limit") or 10)))
    data = api("search.messages", query=query, count=limit, sort="timestamp")
    rows = []
    for m in (data.get("messages") or {}).get("matches") or []:
        rows.append({"channel": (m.get("channel") or {}).get("name", ""),
                     "channel_id": (m.get("channel") or {}).get("id", ""),
                     "user": m.get("username") or user_name(m.get("user")),
                     "text": (m.get("text") or "")[:MAX_TEXT], "ts": m.get("ts", ""),
                     "time": when(m.get("ts")), "permalink": m.get("permalink", "")})
    return {"matches": rows}


def unread(args):
    """Unread counts as Slack reports them to a user token (conversations.info)."""
    limit = max(1, min(30, int(args.get("limit") or 30)))
    rows, reported = [], False
    for c in conversations("public_channel,private_channel,mpim,im", 300):
        if not (c.get("is_member") or c.get("is_im")):
            continue
        info = api("conversations.info", channel=c["id"])["channel"]
        count = info.get("unread_count_display")
        if count is None:
            continue
        reported = True
        if count:
            name = user_name(c.get("user")) if c.get("is_im") else c.get("name", "")
            rows.append({"id": c["id"], "name": name, "unread": count})
        if len(rows) >= limit:
            break
    if not reported:
        raise Failure("Slack ne donne pas les non-lus à ce jeton (il faut un jeton "
                      "utilisateur xoxp-…).")
    return {"unread": rows}


def send(args):
    text = (args.get("text") or "").strip()
    if not text:
        raise Failure("Message vide")
    thread_ts = args.get("thread_ts")
    if thread_ts and not TS.match(str(thread_ts)):
        raise Failure("thread_ts invalide")
    data = api("chat.postMessage", channel=channel_id(args.get("channel")), text=text,
               thread_ts=thread_ts)
    return {"sent": True, "channel": data.get("channel", ""), "ts": data.get("ts", "")}


def react(args):
    ts = str(args.get("ts", ""))
    emoji = (args.get("emoji") or "").strip().strip(":")
    if not TS.match(ts) or not re.match(r"^[a-z0-9_+'-]+$", emoji):
        raise Failure("ts ou emoji invalide (emoji: un nom comme thumbsup)")
    api("reactions.add", channel=channel_id(args.get("channel")), timestamp=ts, name=emoji)
    return {"reacted": emoji}


# --- MCP ---------------------------------------------------------------------------

S, I = {"type": "string"}, {"type": "integer"}
MSG = {"type": "object", "properties": {"ts": S, "time": S, "user": S, "text": S, "replies": I}}
CHANNEL = {"type": "string", "description": "#name or channel id (from slack.channels)."}
TOOLS = {
    "channels": (channels, {
        "description": "Conversations the token sees: public and private channels, group "
                       "and direct messages, with id, name, kind (public/private/dm).",
        "inputSchema": {"type": "object", "properties": {"limit": I}},
    }),
    "history": (history, {
        "description": "Recent messages of a channel, oldest first, with author names and "
                       "reply counts; limit 1 to 50 (default 20), oldest = a ts lower bound.",
        "inputSchema": {"type": "object", "required": ["channel"], "properties": {
            "channel": CHANNEL, "limit": I, "oldest": S}},
        "outputSchema": {"type": "object", "properties": {
            "channel": S, "messages": {"type": "array", "items": MSG}}},
    }),
    "thread": (thread, {
        "description": "A thread: its first message and replies.",
        "inputSchema": {"type": "object", "required": ["channel", "ts"], "properties": {
            "channel": CHANNEL, "ts": S}},
    }),
    "search": (search, {
        "description": "Search messages (Slack search syntax: from:@x in:#y after:2026-10-01); "
                       "user token only; limit 1 to 20.",
        "inputSchema": {"type": "object", "required": ["query"], "properties": {
            "query": S, "limit": I}},
    }),
    "unread": (unread, {
        "description": "Conversations with unread messages and their counts (user token).",
        "inputSchema": {"type": "object", "properties": {"limit": I}},
    }),
    "send": (send, {
        "description": "Post a message as the token's user or bot, optionally in a thread.",
        "inputSchema": {"type": "object", "required": ["channel", "text"], "properties": {
            "channel": CHANNEL, "text": S, "thread_ts": S}},
    }),
    "react": (react, {
        "description": "Add an emoji reaction (name like thumbsup) to a message.",
        "inputSchema": {"type": "object", "required": ["channel", "ts", "emoji"], "properties": {
            "channel": CHANNEL, "ts": S, "emoji": S}},
    }),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Réponse de Slack inattendue ou "
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
                  "serverInfo": {"name": "samantha-slack", "version": "0.1.0"}}
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
