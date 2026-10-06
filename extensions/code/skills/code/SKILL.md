---
name: code
description: Use for the user's GitHub or GitLab: what needs their attention, reviews waiting, their open pull or merge requests, forge notifications, whether CI passes ("j'ai des reviews ?", "la CI est verte ?"). Writing or fixing code is hire.
---
# Code

Every forge the user connected, as one: ids and repos carry their provider (`github:owner/name`, `gitlab:group/project`).

- `code.inbox {limit?}`: notifications and to-dos, newest first (`reason`: review_requested, mention, ci_activity, assigned…). "Qu'est-ce qui m'attend sur GitHub ?".
- `code.reviews {}`: pull/merge requests waiting for their review. `code.mine {}`: their own open ones.
- `code.repos {}`: their repositories; `code.ci {repo}`: its latest CI runs (status: success, failed, running…).
- `code.mark_read {id}`: an inbox item done.

Answering:
- A short triage: what blocks someone else first (reviews requested), then failures (CI), then mentions; one line each with the repo. Offer to open one (`web.open` its `url`).
- "La CI est verte ?": `code.repos` to find the repo, then `code.ci`.
- Never invent a PR, a status or a number.
