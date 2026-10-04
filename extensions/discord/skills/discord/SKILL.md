---
name: discord
description: Read and post on Discord through the user's own bot (official API): servers, channels, recent messages, send, react.
---
# Discord

The bot only sees the servers it was invited to (not the user's private messages), and reads message text only if its Message Content intent is on.

Tools (`channel` is an id, or a name together with `server`):
- `discord.servers {}` → `{servers: [{id, name}]}`.
- `discord.channels {server}` → `{server, channels: [{id, name, kind, category}]}`.
- `discord.messages {channel, server?, limit?, before?}` → `{channel, messages: [{id, time, author, text, attachments}]}`, oldest first. "Quoi de neuf sur le Discord des amis ?".
- `discord.send {channel, server?, text, reply_to?}`: posts as the bot, 2000 characters at most, mentions don't ping. Always asked to the user first.
- `discord.react {channel, server?, message_id, emoji}`: a unicode emoji, or `name:id` for a custom one.

Answering:
- Summarize a channel in a few lines: topics, who asked the user something, plans and dates. An empty `text` usually means the Message Content intent is off: say so once.
- Before `discord.send`, show the exact text and where it goes; never add commitments the user didn't make.
- If a tool says Discord isn't set up, or the bot lacks a permission, pass that on (the message says what to do). Never suggest using the user's account token: Discord bans self-bots.
- Never invent a message, a person or a channel. Answer in the user's language.
