---
name: telegram
description: Use to read or post on Telegram through the user's own bot: what's new in a chat or group, recent messages, send, react ("qu'est-ce qu'on dit dans le groupe Famille ?"). The bot only sees chats where someone wrote to it or that it was added to.
---
# Telegram

Otto uses the user's own bot (Bot API), never their Telegram account. A bot has no history: it only sees messages that arrived since it was in the chat, and Otto keeps the last 200 of each chat.

Setup (if a tool says Telegram isn't set up, walk the user through it):
1. In Telegram, write to @BotFather, `/newbot`, pick a name and a username ending in `bot`.
2. Store the token it gives: `otto provider set-key telegram`.
3. For a group, in @BotFather: `/setprivacy` → the bot → Disable, so it reads every message (otherwise only commands and replies to it). Then add the bot to the group (after changing privacy, remove and re-add it).
4. For a private chat, the user (or the other person) must write to the bot first: a bot can't start a conversation.

Tools (`chat` is an id, a name — case-insensitive — or an @username):
- `telegram.chats {}` → `{chats: [{id, name, type, last}]}`, most recent first.
- `telegram.messages {chat, limit?}` → `{chat, name, messages: [{id, time, from, text, reply_to?, edited?}]}`, oldest first. Media show as `[photo]`, `[voice]`…
- `telegram.send {chat, text, reply_to?}`: plain text as the bot, 4096 characters at most. Always asked to the user first.
- `telegram.react {chat, message_id, emoji}`: one of Telegram's standard emoji (👍 ❤️ 🔥 🎉 😁 🙏 👌…); `""` removes it.

New incoming messages also arrive as `telegram.message` events `{chat, chat_id, from, text, id}`.

Answering:
- Summarize a chat in a few lines: topics, who asked the user something, plans and dates. Say when the history is short because the bot only sees recent messages.
- Before `telegram.send`, show the exact text and the chat it goes to; never add commitments the user didn't make. No HTML or Markdown: it is sent as plain text.
- If a tool says a webhook is set, explain it and only remove it if the user asks (another service may rely on it). If several chats share a name, ask which one.
- Never invent a message, a person or a chat. Never suggest logging in with the user's own account. Answer in the user's language.
