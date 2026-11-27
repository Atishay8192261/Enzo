# ADR 0028 — Helpers: Enzo hands long work to a child agent and stays free to talk

- **Status:** Accepted (2026-11-27).
- **Spec:** §9.5 (subagents: handoff, no polling, inherited safety, one coordinator level, closing
  stops, resume after restart), §9.2 (recovery after a restart), §12.2 (other agents' reports are
  data), §14 "Subagent drift" (the brief carries the limits, the parent reviews before delivery),
  §17 ("long task delegated, main chat stays responsive, child closed cleanly").
- **Builds on:** ADR 0022 (a run is `service.send` in its own chat), ADR 0014 (approvals), ADR 0019
  (logs), ADR 0027 (turn hooks).

## Decision

1. **A helper is an ordinary agent turn in its own chat** (kind `side`, titled "Helper: …"). The
   brain's `delegate` tool takes a `title`, the `task`, the user's `limits` (required: "none
   given" when there are none) and `context`. Enzo adds the user's own words of the message that
   started it, and passes them down to every level. That text is the chat's first message
   (`origin = "brief"`); a note for the brain says it is a helper, the user is not watching, its
   final answer is its report, and whether it may hand work on. Every rule holds: tools validate,
   outside text is fenced, and a gated step waits for the user in the helper's chat.
2. **No polling.** There is no tool to check on a helper. Each new message in a chat says which
   of its helpers are still at work ("never wait or check on them").
3. **Handoff.** When a helper ends, its report is kept on its row, a line goes into the user's
   chat (`helper_notices`), open apps get a `helper` event on `/events`, and a desktop
   notification goes out. The chat that asked gets the report with its next turn, fenced as
   `<untrusted-data source="helper #N report">` under a note that it is a helper's data. When
   that chat is free (no turn running, no open approval), Enzo starts that turn itself
   (`origin = "handoff"`, once per report per gateway run), so the reviewed answer appears
   without the user asking. A report counts as delivered when that turn ended with an answer.
4. **The chat stays free.** Enzo's own turns there are background turns: a message from the user
   waits for one to end (seconds) instead of being refused. A race fix rides with this: a chat is
   claimed before any await, so two messages at once never start two turns.
5. **Nesting stops at two levels**, in code: the user's chat → a helper (it may fan out) → its
   helpers (`delegate` refuses there). A coordinator's own work is done only when its helpers are
   done; their reports come back to it first and it reviews them in one turn.
6. **Closing stops the work at once:** `/helpers/{id}/stop`, the brain's `stop_helper` (only for
   helpers its chat started), or Esc in the helper's chat. The model call in progress is cut off,
   no further step starts (a step already running finishes), its open approval cards go, and the
   helpers it handed work to stop too.
7. **Saved step by step; resumed after a restart.** A helper's turn saves its brief at once and
   every finished step (a `checkpoint` callback in the loop). At start-up a helper that was
   working picks up from its saved transcript (or its brief, when nothing was saved), says so in
   the user's chat, and is reported as interrupted after 2 restarts. Shutdown cancels helper
   turns without saving more, so nothing half-done is kept.
8. **Limits:** at most `ENZO_HELPERS_MAX` (4) working at once, `ENZO_HELPER_STEPS` (30) steps per
   turn. Their model calls go through the user's own accounts and are recorded in the ledger
   under the helper's chat; `/helpers` shows steps, calls and cost. The gateway kept on in the
   background does not stop while helper work runs.
9. **Logs** (numbers only): `child.start`, `child.refused` (reason), `child.waiting`, `child.done`
   (outcome, calls, tools, tokens, cost, ms), `child.stopped` (by, ms), `child.resumed`,
   `child.interrupted`, `child.handoff`.

## Why not

- **The parent waits for the child inside its turn:** the chat would be locked for minutes.
- **Results only on the user's next message:** the user would have to ask for them.
- **A review turn the user's message interrupts:** the transcript would hold a half review; a
  short wait is simpler and loses nothing.
- **Delegation gated by approval:** handing work over sends, buys and deletes nothing; whatever
  the helper would do outward still asks, and the cap bounds the spend.

## Limits (honest)

- A step that is running when a helper is closed (a page loading) finishes; nothing after it.
- A user message waits for a review turn to end; one that uses many tools makes that wait longer.
- Whether a model follows a planted instruction inside a report depends on the model: the code
  guarantees the fence and that any outward action still asks.
- If the user quits while a helper works and nothing is scheduled, the launcher stops the gateway;
  the helper picks up at the next start.
