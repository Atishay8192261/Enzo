# ADR 0007 — Memory rides in the new user message, after the history

- **Status:** Accepted (decided by Atishay Jain, 2026-11-17)
- **Supersedes:** ADR 0004 decision 5 and its trade-off note

## Context

Spec §3 step 3 lists the prompt layers as system, developer, retrieved memory, history, user
message. Retrieved memory changes almost every message. Providers cache prompts by prefix, so
memory placed before the history invalidated the cached history on every turn. Example: a
50-message chat of ~40K tokens on Claude Sonnet 5.5 re-paid ~$0.08 per message instead of ~$0.005.

Newer models also check that the conversation was not edited before their reasoning blocks
(Claude's preserved thinking, OpenAI's prefix rule), so any fix must keep the history append-only.

## Decision

Retrieved memory is an extra text part of the new user message (after the user's words and the
delivery context). It is persisted with that message, so the stored conversation is exactly what
was sent: every turn's prompt extends the previous one, byte for byte.

## Consequences

- Prompt caches cover the whole history; replies are cheaper and start faster.
- Works on every provider: no special mid-conversation system messages.
- Old memory snippets stay in old turns (as they were at the time); the newest sits next to the
  newest message. Clients show only the user's words (`toItems` already hides extra parts).
- A deliberate, recorded departure from the order spec §3 lists. The information sent is the same.
