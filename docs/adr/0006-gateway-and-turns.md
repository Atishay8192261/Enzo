# ADR 0006 — Gateway, conversation service and background turns

- **Status:** Accepted
- **Date:** 2026-11-17

## Decisions

| # | Decision | Why |
|---|---|---|
| 1 | REST + Server-Sent Events on FastAPI's native SSE: `POST /conversations/{id}/turns` and `/resume` stream `text`, `tool_started`, `tool_finished`, `approval_needed`, `done`, `error`. | One gateway for every client (§2.3, §10). SSE works over POST and through proxies; no extra library. |
| 2 | A turn runs as a background task feeding a queue; the HTTP stream only reads it. | If the client disconnects, the agent still finishes and saves the turn (§6.1, §9.2). Tested by closing the stream before the first event. |
| 3 | One running turn per conversation (409 otherwise); the store also locks the row on save. | Keeps the transcript in order. |
| 4 | Message metadata (time, timezone, device) is a second text part of the user's message, not part of the system layer. | §3 step 1 keeps metadata; putting a timestamp in the leading layers would break the providers' prompt cache on every turn. |
| 5 | The system prompt (`enzo/agent/system_prompt.md`) carries Enzo's identity (§0) and the presentation and safety rules (§10.3, §12). | One reviewed, versioned source. |
| 6 | Bearer-token auth when `ENZO_API_TOKEN` is set; `ENZO_ENV=prod` refuses to start without one. `/healthz` stays open. | §3 step 1 "authenticate the user"; safe default for deployments, no friction for local dev. |
| 7 | The router attaches its attempt records to every error it raises; the loop keeps them. | A failed call must be recorded (§1.3.4 step 1). Found by a live run: failures were logged in memory but never persisted. |
| 8 | Tests time out after 30s and CI jobs after 15 min. | A hung test fails loudly instead of stalling CI for hours (found while mutation-testing). |
