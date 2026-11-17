# ADR 0004 — Agent loop, approvals and prompt order

- **Status:** Accepted
- **Date:** 2026-11-17

## Decisions

| # | Decision | Why |
|---|---|---|
| 1 | `run_turn` is an async generator of events (`TextDelta`, `ToolStarted`, `ToolFinished`, `ApprovalNeeded`, `TurnDone`) and always ends with one explicit stop reason. | Clients stream it as-is (§3 step 4). Stop conditions are explicit (§4.2): final answer, approval pending, max steps, refusal, truncated, brain failure, over budget, cancelled. |
| 2 | All tool calls in one reply are independent and run concurrently; their results return in one tool message, in call order. Dependent calls wait for the next step. | §4.2 parallelism; Claude also needs all results of a turn in one message. |
| 3 | Approval id = hash of the tool name and its exact validated arguments. No decision, no execution; a "no" goes back to the brain as a declined result. | §12.2 / §12.3: approval covers only the exact action and content; changed content asks again. |
| 4 | The returned history is always consistent: tool calls at its end are executed first on the next run; calls from a cut-off reply are answered with "not run". | §6.1 resume after interruption without re-asking; a truncated tool input is never executed. |
| 5 | Prompt layers follow §3 step 3 literally: system, developer, retrieved memory, history, user message; tools as API parameters; tool results appended by the loop. | The spec fixes the order. |

## Trade-off to revisit

Decision 5 places per-message memory *before* the history. Claude and OpenAI cache prompts by prefix,
so memory that changes every message invalidates the cached history each turn: correct, but more
expensive. Moving memory after the history keeps the cache warm. That is a one-line change in
`compose_prompt`, held back because it departs from the spec's stated order.
