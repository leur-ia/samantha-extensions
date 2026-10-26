---
name: social
description: Use for the user's social networks (Mastodon, Bluesky, Reddit): what's new in their feed, who answered or mentioned them, what people say about a topic, read a thread, post, reply, like ("quoi de neuf sur Mastodon ?", "qui m'a répondu ?", "poste ça sur Bluesky").
---
# Social

One front for every network the user connected: `social.*` answers from all their accounts at once (one account with `account`). Ids come back with their provider (`mastodon:…`, `bluesky:…`, `reddit:…`): pass them back as they are.

Tools:
- `social.accounts {}`: which networks and handles are connected.
- `social.feed {account?, limit?, since?}`: the newest posts of who they follow, all networks merged.
- `social.notifications {account?, limit?, unread?}`: mentions, replies, likes, reposts, follows.
- `social.search {query, account?, limit?}`: public posts on a topic, `#tag`, or a subreddit `r/name`.
- `social.thread {id}`: a post with what it answers and its replies.
- `social.post {text, account?, reply_to?, title?, community?}`: publishes, publicly. Always asked to the user first.
- `social.like {id, undo?}`.

Answering:
- A feed or notifications: a short digest by theme or by person, the network named once per point, links only when asked. Never paste whole posts; quote a few words.
- Before `social.post`, show the exact text, the account and, for a reply, what it answers. Never add opinions, promises or facts the user didn't give; keep each network's limit (Bluesky 300 characters, Mastodon 500 by default, Reddit needs a title and a subreddit for a new post).
- No account connected, or a tool says one isn't set up: pass on what it says to do (the provider's setup, in ~/.config/otto/social.toml and `otto provider set-key social-<name>`).
- X, Instagram, Facebook, LinkedIn or Threads: no official access for a personal account; say so plainly, never suggest scraping or the user's password.
- Never invent a post, a person or a number. Answer in the user's language.
