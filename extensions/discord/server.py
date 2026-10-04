#!/usr/bin/env python3
"""MCP stdio server of the `discord` extension (stdlib only).

The official Discord REST API v10 with the bot token of the user's own application
(DISCORD_TOKEN in the environment, from the Secret Service). The bot sees only the
servers it was invited to, and message text only with the Message Content intent.
User-account tokens are never used. Failures are `isError` results, never a crash;
nothing is logged and no error carries the token.
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
API = "https://discord.com/api/v10"
TIMEOUT = 10
USER_AGENT = "DiscordBot (https://gitlab.com/jeanbaptiste2/samantha, 0.1.0)"
MAX_SEND = 2000
TEXT_TYPES = {0: "text", 5: "announcement", 15: "forum"}
SNOWFLAKE = re.compile(r"^\d{15,22}$")
SETUP = ("Discord n'est pas configuré: crée une application sur "
         "discord.com/developers/applications, ajoute un bot, active l'intent Message "
         "Content, invite-le sur tes serveurs avec les permissions Voir les salons, Lire "
         "l'historique, Envoyer des messages et Ajouter des réactions, puis enregistre son "
         "jeton avec `samantha provider set-key discord`. (Un jeton de compte utilisateur "
         "n'est jamais utilisé: c'est interdit par Discord.)")


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def token():
    tok = os.environ.get("DISCORD_TOKEN", "").strip()
    if not tok:
        raise Failure(SETUP)
    return tok.removeprefix("Bot ").strip()


def api(method, path, body=None):
    """One REST call, its JSON (None for 204); one retry after a 429."""
    data = json.dumps(body).encode() if body is not None else None
    for attempt in (0, 1):
        req = urllib.request.Request(API + path, data=data, method=method, headers={
            "Authorization": f"Bot {token()}", "User-Agent": USER_AGENT,
            "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                try:
                    wait = float(json.load(e).get("retry_after", 1))
                except (ValueError, AttributeError, OSError):
                    wait = 1.0
                time.sleep(min(wait, 5))
                continue
            if e.code == 401:
                raise Failure("Discord refuse le jeton du bot. " + SETUP)
            if e.code == 403:
                raise Failure("Le bot n'a pas la permission (salon caché, permission ou "
                              "intent Message Content manquant).")
            if e.code == 404:
                raise Failure("Introuvable (ou le bot n'y a pas accès).")
            raise Failure(f"Discord a refusé la requête ({e.code})")
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Failure("Discord injoignable (réseau ?)")
        except ValueError:
            raise Failure("Discord a répondu quelque chose d'illisible")
    raise Failure("Discord limite les requêtes: réessaie dans un moment")


def pick(ref, items, what):
    """An item by id or case-insensitive name; ambiguity lists the candidates."""
    ref = (ref or "").strip().lstrip("#")
    if not ref:
        raise Failure(f"Donne un {what} (nom ou identifiant)")
    if SNOWFLAKE.match(ref):
        return ref
    found = [i for i in items if (i.get("name") or "").lower() == ref.lower()]
    if not found:
        raise Failure(f"{what.capitalize()} introuvable: {ref}")
    if len(found) > 1:
        ids = ", ".join(i["id"] for i in found)
        raise Failure(f"Plusieurs {what}s s'appellent {ref}: {ids} (donne l'identifiant)")
    return found[0]["id"]


def guilds():
    return api("GET", "/users/@me/guilds") or []


def servers(args):
    return {"servers": [{"id": g["id"], "name": g.get("name", "")} for g in guilds()]}


def channels(args):
    gid = pick(args.get("server"), guilds(), "serveur")
    every = api("GET", f"/guilds/{gid}/channels") or []
    categories = {c["id"]: c.get("name", "") for c in every if c.get("type") == 4}
    rows = [{"id": c["id"], "name": c.get("name", ""), "kind": TEXT_TYPES[c["type"]],
             "category": categories.get(c.get("parent_id"), "")}
            for c in sorted(every, key=lambda c: c.get("position", 0))
            if c.get("type") in TEXT_TYPES]
    return {"server": gid, "channels": rows}


def channel_id(args):
    """A channel id, or a name resolved within `server`."""
    ref = (args.get("channel") or "").strip().lstrip("#")
    if SNOWFLAKE.match(ref):
        return ref
    if not args.get("server"):
        raise Failure("Pour un salon donné par son nom, précise aussi le serveur")
    return pick(ref, channels(args)["channels"], "salon")


def messages(args):
    cid = channel_id(args)
    limit = max(1, min(50, int(args.get("limit") or 20)))
    query = {"limit": limit}
    if args.get("before"):
        if not SNOWFLAKE.match(str(args["before"])):
            raise Failure("before invalide: un identifiant de message")
        query["before"] = args["before"]
    got = api("GET", f"/channels/{cid}/messages?{urllib.parse.urlencode(query)}") or []
    rows = []
    for m in reversed(got):  # oldest first
        author = m.get("author") or {}
        member = m.get("member") or {}
        rows.append({"id": m["id"], "time": (m.get("timestamp") or "")[:16].replace("T", " "),
                     "author": member.get("nick") or author.get("global_name")
                     or author.get("username", ""),
                     "text": m.get("content", ""),
                     "attachments": [a.get("filename", "") for a in m.get("attachments") or []]})
    return {"channel": cid, "messages": rows}


def send(args):
    text = (args.get("text") or "").strip()
    if not text:
        raise Failure("Message vide")
    if len(text) > MAX_SEND:
        raise Failure(f"Message trop long ({len(text)} caractères, {MAX_SEND} au plus)")
    body = {"content": text, "allowed_mentions": {"parse": []}}
    if args.get("reply_to"):
        if not SNOWFLAKE.match(str(args["reply_to"])):
            raise Failure("reply_to invalide: un identifiant de message")
        body["message_reference"] = {"message_id": args["reply_to"]}
    m = api("POST", f"/channels/{channel_id(args)}/messages", body)
    return {"sent": True, "id": (m or {}).get("id", "")}


def react(args):
    mid = str(args.get("message_id", ""))
    emoji = (args.get("emoji") or "").strip().strip(":")
    if not SNOWFLAKE.match(mid) or not emoji:
        raise Failure("message_id ou emoji invalide")
    # Unicode emoji as is, custom ones as name:id; both percent-encoded in the path.
    path = f"/channels/{channel_id(args)}/messages/{mid}/reactions/" \
           f"{urllib.parse.quote(emoji, safe='')}/@me"
    api("PUT", path)
    return {"reacted": emoji}


# --- MCP ---------------------------------------------------------------------------

S, I = {"type": "string"}, {"type": "integer"}
CHANNEL = {"type": "string", "description": "Channel id, or its name with `server`."}
SERVER = {"type": "string", "description": "Server id or name (from discord.servers)."}
TOOLS = {
    "servers": (servers, {
        "description": "Servers the bot is in: id and name.",
        "inputSchema": {"type": "object", "properties": {}},
    }),
    "channels": (channels, {
        "description": "Text, announcement and forum channels of a server, with categories.",
        "inputSchema": {"type": "object", "required": ["server"], "properties": {"server": SERVER}},
    }),
    "messages": (messages, {
        "description": "Recent messages of a channel, oldest first: author, text, attachment "
                       "names; limit 1 to 50 (default 20), before = a message id.",
        "inputSchema": {"type": "object", "required": ["channel"], "properties": {
            "channel": CHANNEL, "server": SERVER, "limit": I, "before": S}},
    }),
    "send": (send, {
        "description": "Post a message as the bot (2000 characters at most), optionally as "
                       "a reply; mentions don't ping.",
        "inputSchema": {"type": "object", "required": ["channel", "text"], "properties": {
            "channel": CHANNEL, "server": SERVER, "text": S, "reply_to": S}},
    }),
    "react": (react, {
        "description": "Add a reaction: a unicode emoji, or name:id for a custom one.",
        "inputSchema": {"type": "object", "required": ["channel", "message_id", "emoji"],
                        "properties": {"channel": CHANNEL, "server": SERVER,
                                       "message_id": S, "emoji": S}},
    }),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Réponse de Discord inattendue ou "
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
                  "serverInfo": {"name": "samantha-discord", "version": "0.1.0"}}
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
