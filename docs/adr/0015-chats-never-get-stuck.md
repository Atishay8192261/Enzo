# ADR 0015 — A chat never gets stuck

- **Status:** Accepted (2026-11-19).
- **Issues:** #46 (a Wi-Fi drop locked a chat for 10 minutes), #39 (closing the app during an approval
  broke the chat for good).
- **Spec:** §4 (agent loop, stop conditions), §1.3.4 (brain failures), §11 (persistence), §12.3

## Decisions

1. **Brain HTTP finds a dead network fast** (`enzo.brain.transport`).
   - TCP keepalive: 10 s idle, then a probe every 5 s, with 3 probes. A dead link fails in about
     25 s. A model that is only thinking still answers the probes, so it is never cut off.
   - A read timeout of 300 s is the backstop for a server that is alive but stuck. That is the
     same idle limit as OpenAI's Codex.
   - Connect timeout 15 s.
   - All replace the SDKs' 600 s read and 5 s connect.
2. **Nothing that streamed is thrown away.** When a reply breaks off (lost connection, Stop), the
   words the user already saw are saved as an assistant message, and the turn says why it ended.
   Apps keep the same words if their own stream drops.
3. **Stop** (`POST /conversations/{id}/stop`; Esc in the terminal, a Stop button on the web).
   - It ends the reply being written, from any window, even one that never saw the stream.
   - It works inside the brain call: `asyncio.timeout` is rescheduled to now. So the HTTP stream is
     cancelled and closed by the task that opened it.
   - Tools that are already running finish first, so tool state stays consistent.
   - The cancelled attempt is still logged to the ledger (§1.3.5).
4. **A turn still outlives its client.** Closing the app mid-reply does not cancel the turn; the
   reply is saved, as before (§6.1). A message sent while a reply runs is refused (409) and the apps
   say "not sent" in plain words, offering Stop. There is no queue: the user decides.
5. **An open approval never breaks the history.**
   - A new message while a card is open closes its calls as "not run (the user sent a new message)".
     It also drops the unanswered approval, so nothing runs unasked and a late "yes" runs nothing.
   - Every provider then sees a valid history.
   - Chats broken before this fix are repaired when the prompt is built (missing results are
     added). The stored transcript is not rewritten.
6. **A reopened app shows open cards.**
   - Approvals store their call id, summary and detail (migration 0003).
   - `GET /conversations/{id}` returns the cards waiting on the calls the chat ends with, and
     whether a reply is running.

## Consequences

- A dead network ends a reply in about half a minute with the partial text kept, instead of
  locking the chat for 10 minutes.
- **Not covered:**
  - A model that streams bytes but never finishes is bounded only by the 300 s backstop, or by Stop.
  - Anthropic's own read timeout is replaced too: long Claude replies keep streaming, so the
    300 s limit only matters if nothing arrives at all.
