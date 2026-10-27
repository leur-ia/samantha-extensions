#!/usr/bin/env python3
"""MCP stdio server of the `telegram` extension (stdlib only).

The official Telegram Bot API with the token of the user's own bot (TELEGRAM_TOKEN in
the environment, from the Secret Service). Never a user account (no MTProto userbot).

The Bot API has no chat list and no history: a bot only sees updates. So the server
keeps what it saw in ~/.local/state/otto/telegram/store.json (owner-only): the chats
(id, name, type, last message time), the last MAX_KEPT messages of each, and the
getUpdates offset. A background thread long-polls getUpdates from MCP initialize on,
each tool call syncs too, and each new incoming message is published as a
`telegram.message` event. getUpdates can't run while a webhook is set: that is reported,
never undone here. Failures are `isError` results, never a crash; no error carries the
token.
"""

import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

PROTOCOL = "2025-06-18"
API = "https://api.telegram.org/bot"
TIMEOUT = 10
POLL = 25          # getUpdates long-poll, seconds
MAX_SEND = 4096    # UTF-16 code units, as Telegram counts
MAX_KEPT = 200     # messages kept per chat
MAX_EVENTS = 10    # events published per sync, newest kept
EXCERPT = 200
CHAT_ID = re.compile(r"^-?\d{1,20}$")
MEDIA = ("photo", "video", "voice", "audio", "document", "sticker", "animation",
         "video_note", "location", "contact", "poll")
SETUP = ("Telegram n'est pas configuré: dans Telegram, écris à @BotFather, `/newbot`, "
         "choisis un nom, puis enregistre le jeton qu'il te donne avec `otto provider "
         "set-key telegram`. Écris ensuite au bot (ou ajoute-le à un groupe) pour qu'il "
         "voie la conversation. (Un compte utilisateur n'est jamais utilisé: seulement "
         "ton bot.)")
WEBHOOK = ("Un webhook est configuré sur ce bot: Telegram ne donne alors pas les messages "
           "à Otto. Si tu veux qu'Otto les lise, retire-le (méthode deleteWebhook de l'API "
           "Bot, par exemple `curl https://api.telegram.org/bot<jeton>/deleteWebhook`); "
           "l'autre service branché sur ce webhook ne recevra plus rien.")


class Failure(Exception):
    """A message for the model: the call failed, say why."""


def token():
    tok = os.environ.get("TELEGRAM_TOKEN", "").strip()
    if not tok:
        raise Failure(SETUP)
    return tok


def api(method, body=None, wait=0):
    """One Bot API call, its `result`; one retry after a 429."""
    data = json.dumps(body or {}).encode()
    for attempt in (0, 1):
        req = urllib.request.Request(API + token() + "/" + method, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT + wait) as r:
                return json.loads(r.read())["result"]
        except urllib.error.HTTPError as e:
            try:
                err = json.load(e)
            except (ValueError, OSError):
                err = {}
            desc = str(err.get("description") or "")
            if e.code == 429 and attempt == 0:
                wait_s = (err.get("parameters") or {}).get("retry_after", 1)
                time.sleep(min(float(wait_s), 5))
                continue
            if e.code in (401, 404):
                raise Failure("Telegram refuse le jeton du bot. " + SETUP)
            if e.code == 409:
                if "webhook" in desc.lower():
                    raise Failure(WEBHOOK)
                raise Failure("Un autre programme lit déjà les messages de ce bot "
                              "(une seule lecture à la fois): arrête-le, ou utilise un "
                              "autre bot pour Otto.")
            if e.code == 403:
                raise Failure("Le bot n'a pas accès à cette conversation (bloqué, retiré "
                              "du groupe, ou on ne lui a jamais écrit).")
            if e.code == 400 and "chat not found" in desc.lower():
                raise Failure("Conversation introuvable: la personne doit d'abord écrire "
                              "au bot, ou le bot être ajouté au groupe.")
            raise Failure(f"Telegram a refusé la requête ({e.code}{': ' + desc if desc else ''})")
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Failure("Telegram injoignable (réseau ?)")
        except (ValueError, KeyError, TypeError):
            raise Failure("Telegram a répondu quelque chose d'illisible")
    raise Failure("Telegram limite les requêtes: réessaie dans un moment")


# --- the store of what the bot saw -------------------------------------------------

_store_lock = threading.Lock()   # the store file
_poll_lock = threading.Lock()    # one getUpdates at a time (Telegram answers 409 otherwise)


def store_path():
    # The path the sandbox grants (`[needs] write`), not $XDG_STATE_HOME; TELEGRAM_STATE
    # for the tests.
    if os.environ.get("TELEGRAM_STATE"):
        return os.environ["TELEGRAM_STATE"]
    return os.path.expanduser("~/.local/state/otto/telegram/store.json")


def load():
    try:
        with open(store_path()) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {"offset": data.get("offset"), "chats": data.get("chats") or {},
                    "messages": data.get("messages") or {}}
    except (OSError, ValueError):
        pass
    return {"offset": None, "chats": {}, "messages": {}}


def save(data):
    path = store_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def person(u):
    u = u or {}
    name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x)
    return name or u.get("username") or str(u.get("id", ""))


def chat_name(c):
    return c.get("title") or person(c)


def text_of(m):
    text = m.get("text") or m.get("caption") or ""
    kind = next((k for k in MEDIA if k in m), None)
    if kind == "sticker":
        kind = "sticker " + ((m.get("sticker") or {}).get("emoji") or "")
    return f"[{kind.strip()}] {text}".strip() if kind else text


def stamp(unix):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(unix or 0))


def remember(data, m, author=None):
    """Adds (or, edited, updates) message `m` in the store; its row."""
    c = m["chat"]
    cid = str(c["id"])
    chat = data["chats"].setdefault(cid, {"id": c["id"]})
    chat.update(name=chat_name(c), type=c.get("type", ""), username=c.get("username", ""))
    if author is None:
        author = person(m["from"]) if m.get("from") else \
            m.get("author_signature") or chat_name(m.get("sender_chat") or c)
    row = {"id": m["message_id"], "time": stamp(m.get("date")), "from": author,
           "text": text_of(m)}
    if (m.get("reply_to_message") or {}).get("message_id"):
        row["reply_to"] = m["reply_to_message"]["message_id"]
    rows = data["messages"].setdefault(cid, [])
    for i, old in enumerate(rows):
        if old["id"] == row["id"]:
            rows[i] = {**row, "time": old["time"], "edited": True}
            return rows[i]
    rows.append(row)
    del rows[:-MAX_KEPT]
    chat["last"] = max(chat.get("last") or 0, m.get("date") or 0)
    return row


def ingest(data, updates):
    """Stores `updates`, moves the offset past them; the events to publish."""
    first = data["offset"] is None
    events = []
    for u in updates:
        data["offset"] = max(data["offset"] or 0, u["update_id"] + 1)
        new = u.get("message") or u.get("channel_post")
        edited = u.get("edited_message") or u.get("edited_channel_post")
        if new:
            row = remember(data, new)
            events.append({"chat": chat_name(new["chat"]), "chat_id": new["chat"]["id"],
                           "from": row["from"], "text": row["text"][:EXCERPT],
                           "id": row["id"]})
        elif edited:
            remember(data, edited)
        elif (u.get("my_chat_member") or {}).get("chat"):  # added to (or left) a group
            c = u["my_chat_member"]["chat"]
            data["chats"].setdefault(str(c["id"]), {"id": c["id"]}).update(
                name=chat_name(c), type=c.get("type", ""), username=c.get("username", ""))
    # The first sync only catches up on the backlog: no burst of old news.
    return [] if first else events[-MAX_EVENTS:]


def daemon(method, params, timeout=5):
    """One request on the daemon socket under the `extension` role."""
    path, tok = os.environ.get("OTTO_SOCKET"), os.environ.get("OTTO_EXTENSION_TOKEN")
    if not path or not tok:
        raise Failure("Le démon de Otto n'est pas joignable")
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(timeout)
        s.connect(path)
        f = s.makefile("rwb")
        for frame in ({"v": 1, "kind": "hello", "role": "extension", "token": tok},
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


def publish(data):
    try:
        daemon("events.publish", {"type": "telegram.message", "data": data})
    except (Failure, OSError, ValueError):
        pass


def sync(wait=0, block=False):
    """Fetches new updates into the store. A tool call (block=False) skips it while the
    background poll is in flight: that poll returns as soon as anything arrives."""
    if not _poll_lock.acquire(blocking=block):
        return
    try:
        with _store_lock:
            offset = load()["offset"]
        body = {"timeout": wait}
        if offset is not None:
            body["offset"] = offset
        updates = api("getUpdates", body, wait=wait)
        with _store_lock:
            data = load()
            events = ingest(data, updates or [])
            save(data)
    finally:
        _poll_lock.release()
    for e in events:
        publish(e)


def poll_forever(stop):
    """Long-polls for the server's lifetime; not set up, webhook, bad token or no
    network: look again in a minute, quietly (the tools say why)."""
    backoff = 2
    while not stop.is_set():
        try:
            sync(wait=POLL, block=True)
            backoff = 2
        except Failure:
            stop.wait(60)
        except Exception as e:
            print(f"telegram poll: {type(e).__name__}", file=sys.stderr, flush=True)
            stop.wait(backoff)
            backoff = min(backoff * 2, 300)


_poller = None


def start_poller():
    global _poller
    if _poller is None:
        _poller = threading.Thread(target=poll_forever, args=(threading.Event(),), daemon=True)
        _poller.start()


# --- tools -------------------------------------------------------------------------

def pick(ref, chats):
    """A chat id by id, case-insensitive name or @username; ambiguity lists candidates."""
    ref = str(ref if ref is not None else "").strip()
    if not ref:
        raise Failure("Donne une conversation (nom ou identifiant)")
    if CHAT_ID.match(ref):
        return int(ref)
    low = ref.lower()
    found = [c for c in chats.values() if (c.get("name") or "").lower() == low
             or (c.get("username") and "@" + c["username"].lower() == low)]
    if not found:
        known = ", ".join(sorted(c.get("name", "") for c in chats.values()))
        raise Failure(f"Conversation introuvable: {ref}. " + (
            f"Le bot connaît: {known}." if known else
            "Le bot n'a encore vu aucune conversation: écris-lui d'abord, ou ajoute-le au groupe."))
    if len(found) > 1:
        ids = ", ".join(f"{c['id']} ({c.get('type', '')})" for c in found)
        raise Failure(f"Plusieurs conversations s'appellent {ref}: {ids} (donne l'identifiant)")
    return found[0]["id"]


def quiet_sync():
    try:
        sync()
    except Failure:
        pass  # sending works even with a webhook set


def chats(args):
    sync()
    rows = sorted(load()["chats"].values(), key=lambda c: c.get("last") or 0, reverse=True)
    out = {"chats": [{"id": c["id"], "name": c.get("name", ""), "type": c.get("type", ""),
                      "last": stamp(c["last"]) if c.get("last") else ""} for c in rows]}
    if not rows:
        out["note"] = ("Aucune conversation vue: écris d'abord au bot, ou ajoute-le à un "
                       "groupe (et coupe /setprivacy chez @BotFather pour qu'il lise tout).")
    return out


def messages(args):
    sync()
    data = load()
    cid = pick(args.get("chat"), data["chats"])
    limit = max(1, min(MAX_KEPT, int(args.get("limit") or 20)))
    rows = data["messages"].get(str(cid), [])[-limit:]
    return {"chat": cid, "name": data["chats"].get(str(cid), {}).get("name", ""),
            "messages": rows}


def message_id(value, what):
    if not re.match(r"^\d{1,12}$", str(value if value is not None else "")):
        raise Failure(f"{what} invalide: un identifiant de message")
    return int(value)


def send(args):
    text = (args.get("text") or "").strip()
    if not text:
        raise Failure("Message vide")
    size = len(text.encode("utf-16-le")) // 2
    if size > MAX_SEND:
        raise Failure(f"Message trop long ({size} caractères, {MAX_SEND} au plus)")
    quiet_sync()
    cid = pick(args.get("chat"), load()["chats"])
    # Plain text only: no parse_mode, so nothing the model writes becomes markup.
    body = {"chat_id": cid, "text": text}
    if args.get("reply_to") not in (None, ""):
        body["reply_parameters"] = {"message_id": message_id(args["reply_to"], "reply_to"),
                                    "allow_sending_without_reply": True}
    m = api("sendMessage", body)
    with _store_lock:
        data = load()
        remember(data, m, author="bot")
        save(data)
    return {"sent": True, "chat": cid, "id": m.get("message_id")}


def react(args):
    mid = message_id(args.get("message_id"), "message_id")
    emoji = (args.get("emoji") or "").strip()
    quiet_sync()
    cid = pick(args.get("chat"), load()["chats"])
    reaction = [{"type": "emoji", "emoji": emoji}] if emoji else []
    try:
        api("setMessageReaction", {"chat_id": cid, "message_id": mid, "reaction": reaction})
    except Failure as e:
        if "REACTION_INVALID" in str(e):
            raise Failure(f"Telegram n'accepte pas {emoji} comme réaction (seulement sa liste "
                          "d'émojis, par exemple 👍 ❤️ 🔥 🎉 😁 🙏 👌)")
        raise
    return {"reacted": emoji, "removed": not emoji}


# --- MCP ---------------------------------------------------------------------------

S, I = {"type": "string"}, {"type": "integer"}
CHAT = {"type": "string", "description": "Chat id, name (case-insensitive) or @username, "
                                         "from telegram.chats."}
TOOLS = {
    "chats": (chats, {
        "description": "Chats the bot has seen (private, groups, channels), most recent "
                       "first: id, name, type, last message time.",
        "inputSchema": {"type": "object", "properties": {}},
    }),
    "messages": (messages, {
        "description": "Messages the bot saw in a chat, oldest first (it keeps the last "
                       "200): id, time, from, text; limit 1 to 200 (default 20).",
        "inputSchema": {"type": "object", "required": ["chat"],
                        "properties": {"chat": CHAT, "limit": I}},
    }),
    "send": (send, {
        "description": "Send a plain-text message as the bot (4096 characters at most), "
                       "optionally as a reply to a message id.",
        "inputSchema": {"type": "object", "required": ["chat", "text"], "properties": {
            "chat": CHAT, "text": S, "reply_to": I}},
    }),
    "react": (react, {
        "description": "Set the bot's reaction on a message (one of Telegram's standard "
                       "emoji); an empty emoji removes it.",
        "inputSchema": {"type": "object", "required": ["chat", "message_id", "emoji"],
                        "properties": {"chat": CHAT, "message_id": I, "emoji": S}},
    }),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return {"content": [{"type": "text", "text": "Réponse de Telegram inattendue ou "
                             "argument invalide"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out, "isError": False}


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None  # notification
    params = msg.get("params") or {}
    if method == "initialize":
        start_poller()
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "otto-telegram", "version": "0.1.1"}}
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
