---
name: slack
description: Read and write the user's Slack (official Web API, their own app's token): channels, recent messages, threads, search, unread, send, react.
---
# Slack

Tools (`channel` is `#name` or an id from `slack.channels`):
- `slack.channels {limit?}` → `{channels: [{id, name, kind, member}]}` (`kind`: public, private, dm).
- `slack.history {channel, limit?, oldest?}` → `{channel, messages: [{ts, time, user, text, replies}]}`, oldest first. "Quoi de neuf sur #general ?".
- `slack.thread {channel, ts}`: a message and its replies (`replies` > 0 in history).
- `slack.search {query, limit?}`: Slack search syntax (`from:@ana in:#projet after:2026-10-01`). User token only.
- `slack.unread {}` → `{unread: [{id, name, unread}]}`. "J'ai des messages Slack ?". User token only.
- `slack.send {channel, text, thread_ts?}`: posts as the user. Always asked to the user first.
- `slack.react {channel, ts, emoji}`: emoji by name (`thumbsup`, `eyes`, `white_check_mark`).

Answering:
- Summaries: who said what, decisions, questions addressed to the user; a few lines, not a transcript. Quote only short bits.
- Before `slack.send`, show the exact text and the channel; write in the user's language and tone, never adding commitments they didn't make.
- If a tool says Slack isn't set up, a scope is missing or a user token is needed, pass that on plainly (the message says what to do) and stop.
- Never invent a message, a person or a channel.
