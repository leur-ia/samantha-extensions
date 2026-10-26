"""cd extensions/telegram && python3 -m unittest -q"""

import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

import server

TOKEN = "123:s3cret"
ANA, FAMILLE, AMIS = 1001, -2002, -3003


class Telegram:
    """Stands in for urlopen: answers Bot API methods from a table, records requests."""

    def __init__(self, answers):
        self.answers, self.requests = answers, []

    def __call__(self, req, timeout=None):
        assert timeout
        assert req.full_url.startswith(server.API + TOKEN + "/")
        method = req.full_url.rsplit("/", 1)[1]
        self.requests.append((method, json.loads(req.data)))
        answer = self.answers[method]
        if callable(answer):
            answer = answer()
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(json.dumps({"ok": True, "result": answer}).encode())


def refused(code, description, **extra):
    body = json.dumps({"ok": False, "error_code": code, "description": description, **extra})
    return urllib.error.HTTPError("u", code, "no", {}, io.BytesIO(body.encode()))


def msg(mid, chat, text, sender="Ana", date=1_790_000_000):
    return {"message_id": mid, "date": date, "chat": chat, "text": text,
            "from": {"id": 9, "first_name": sender}}


PRIVATE = {"id": ANA, "type": "private", "first_name": "Ana", "username": "ana_b"}
GROUP = {"id": FAMILLE, "type": "group", "title": "Famille"}
UPDATES = [{"update_id": 10, "message": msg(1, PRIVATE, "salut")},
           {"update_id": 11, "message": msg(2, GROUP, "dîner dimanche ?", "Bob")},
           {"update_id": 12, "edited_message": {**msg(2, GROUP, "dîner samedi ?", "Bob")}}]


def call(name, arguments):
    r = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
    return r["result"]


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        p = mock.patch.dict(os.environ, {"TELEGRAM_TOKEN": TOKEN,
                                         "TELEGRAM_STATE": os.path.join(self.dir.name, "otto", "telegram", "store.json")})
        p.start()
        self.addCleanup(p.stop)
        self.published = []
        d = mock.patch.object(server, "daemon",
                              lambda method, params, **k: self.published.append((method, params)))
        d.start()
        self.addCleanup(d.stop)

    def run_with(self, answers, name, args):
        t = Telegram({"getUpdates": [], **answers})
        with mock.patch("urllib.request.urlopen", t):
            return call(name, args), t

    def seed(self, updates=UPDATES):
        return self.run_with({"getUpdates": updates}, "chats", {})


class Store(Base):
    def test_get_updates_stores_chats_messages_offset(self):
        r, t = self.seed()
        chats = r["structuredContent"]["chats"]
        self.assertEqual({c["name"] for c in chats}, {"Ana", "Famille"})
        self.assertEqual(t.requests[0], ("getUpdates", {"timeout": 0}))
        with open(server.store_path()) as f:
            self.assertEqual(json.load(f)["offset"], 13)
        self.assertEqual(os.stat(server.store_path()).st_mode & 0o777, 0o600)
        r, t = self.run_with({}, "messages", {"chat": "famille"})
        rows = r["structuredContent"]["messages"]
        self.assertEqual([(m["from"], m["text"], m.get("edited")) for m in rows],
                         [("Bob", "dîner samedi ?", True)])
        self.assertEqual(t.requests[0], ("getUpdates", {"timeout": 0, "offset": 13}))

    def test_keeps_the_last_200_per_chat(self):
        many = [{"update_id": i, "message": msg(i, PRIVATE, f"m{i}")} for i in range(1, 251)]
        self.seed(many)
        r, _ = self.run_with({}, "messages", {"chat": ANA, "limit": 999})
        rows = r["structuredContent"]["messages"]
        self.assertEqual((len(rows), rows[0]["text"], rows[-1]["text"]), (200, "m51", "m250"))

    def test_empty_store_says_how_to_start(self):
        r, _ = self.seed([])
        self.assertIn("écris d'abord au bot", r["structuredContent"]["note"])


class Events(Base):
    def test_new_messages_are_published_not_the_backlog(self):
        self.seed()
        self.assertEqual(self.published, [])  # first sync: catching up only
        later = [{"update_id": 13, "message": msg(3, GROUP, "x" * 500, "Bob")},
                 {"update_id": 14, "edited_message": msg(3, GROUP, "corrigé", "Bob")}]
        self.seed(later)
        self.assertEqual(len(self.published), 1)
        method, params = self.published[0]
        self.assertEqual(method, "events.publish")
        self.assertEqual(params["type"], "telegram.message")
        self.assertEqual(params["data"], {"chat": "Famille", "chat_id": FAMILLE, "from": "Bob",
                                          "text": "x" * 200, "id": 3})

    def test_initialize_starts_one_poller(self):
        with mock.patch.object(server, "_poller", None), \
             mock.patch("threading.Thread") as thread:
            server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
            server.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize"})
        self.assertEqual(thread.call_count, 1)


class Resolve(Base):
    def test_by_name_username_id_and_ambiguous(self):
        twin = {"id": AMIS, "type": "supergroup", "title": "FAMILLE"}
        self.seed(UPDATES + [{"update_id": 20, "message": msg(5, twin, "hé")}])
        for ref in ("ana", "@ANA_B", str(ANA)):
            r, _ = self.run_with({}, "messages", {"chat": ref})
            self.assertEqual(r["structuredContent"]["chat"], ANA, ref)
        r, _ = self.run_with({}, "messages", {"chat": "famille"})
        self.assertTrue(r["isError"])
        text = r["content"][0]["text"]
        self.assertIn("Plusieurs", text)
        self.assertIn(str(FAMILLE), text)
        self.assertIn(str(AMIS), text)
        r, _ = self.run_with({}, "messages", {"chat": "Inconnu"})
        self.assertIn("Le bot connaît", r["content"][0]["text"])


class Send(Base):
    def test_plain_text_reply_and_stored(self):
        self.seed()
        sent = {**msg(7, PRIVATE, "oui !"), "from": {"id": 1, "is_bot": True, "first_name": "B"}}
        r, t = self.run_with({"sendMessage": sent}, "send",
                             {"chat": "Ana", "text": "oui !", "reply_to": 1})
        self.assertEqual(r["structuredContent"], {"sent": True, "chat": ANA, "id": 7})
        method, body = t.requests[-1]
        self.assertEqual(method, "sendMessage")
        self.assertNotIn("parse_mode", body)
        self.assertEqual(body["reply_parameters"]["message_id"], 1)
        r, _ = self.run_with({}, "messages", {"chat": "Ana"})
        self.assertEqual(r["structuredContent"]["messages"][-1]["from"], "bot")

    def test_too_long_fails_before_sending(self):
        r, t = self.run_with({}, "send", {"chat": ANA, "text": "😀" * 2049})  # 4098 UTF-16 units
        self.assertTrue(r["isError"])
        self.assertIn("trop long", r["content"][0]["text"])
        self.assertEqual(t.requests, [])

    def test_send_works_despite_a_webhook(self):
        hook = refused(409, "Conflict: can't use getUpdates method while webhook is active")
        r, _ = self.run_with({"getUpdates": hook, "sendMessage": msg(8, PRIVATE, "ok")},
                             "send", {"chat": ANA, "text": "ok"})
        self.assertFalse(r["isError"])


class React(Base):
    def test_react_and_remove(self):
        r, t = self.run_with({"setMessageReaction": True}, "react",
                             {"chat": ANA, "message_id": 1, "emoji": "👍"})
        self.assertFalse(r["isError"])
        self.assertEqual(t.requests[-1][1], {"chat_id": ANA, "message_id": 1,
                                             "reaction": [{"type": "emoji", "emoji": "👍"}]})
        r, t = self.run_with({"setMessageReaction": True}, "react",
                             {"chat": ANA, "message_id": 1, "emoji": ""})
        self.assertEqual(t.requests[-1][1]["reaction"], [])

    def test_invalid_reaction(self):
        bad = refused(400, "Bad Request: REACTION_INVALID")
        r, _ = self.run_with({"setMessageReaction": bad}, "react",
                             {"chat": ANA, "message_id": 1, "emoji": "🦄"})
        self.assertIn("n'accepte pas", r["content"][0]["text"])


class Errors(Base):
    def test_missing_token(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_TOKEN": ""}):
            r, t = self.run_with({}, "chats", {})
        self.assertTrue(r["isError"])
        self.assertIn("@BotFather", r["content"][0]["text"])
        self.assertIn("set-key telegram", r["content"][0]["text"])
        self.assertEqual(t.requests, [])

    def test_rejected_token_never_leaks(self):
        r, _ = self.run_with({"getUpdates": refused(401, "Unauthorized")}, "chats", {})
        self.assertIn("refuse le jeton", r["content"][0]["text"])
        self.assertNotIn(TOKEN, r["content"][0]["text"])

    def test_webhook_and_other_reader_conflicts(self):
        hook = refused(409, "Conflict: can't use getUpdates method while webhook is active; "
                            "use deleteWebhook to delete the webhook first")
        r, t = self.run_with({"getUpdates": hook}, "chats", {})
        self.assertIn("webhook est configuré", r["content"][0]["text"])
        self.assertEqual([m for m, _ in t.requests], ["getUpdates"])  # never deleteWebhook
        other = refused(409, "Conflict: terminated by other getUpdates request")
        r, _ = self.run_with({"getUpdates": other}, "chats", {})
        self.assertIn("Un autre programme", r["content"][0]["text"])

    def test_retry_once_after_429(self):
        answers = iter([refused(429, "Too Many Requests", parameters={"retry_after": 0}), []])
        r, t = self.run_with({"getUpdates": lambda: next(answers)}, "chats", {})
        self.assertFalse(r["isError"])
        self.assertEqual(len(t.requests), 2)


if __name__ == "__main__":
    unittest.main()
