# ADR 0025 — Evals and traces: Enzo measures itself

- **Status:** Accepted (2026-11-25).
- **Spec:** §1.4, §13 (logging, evals, cost, regression), §15.1, §15.4, §15.5, §17 (swappable
  brain, observability, evals rows). Builds on ADR 0003 (cost), 0005 (ledgers), 0010 (routing),
  0019 (JSON log).

## Decision

1. **A fixed, versioned suite of 20 tasks** (`enzo.evals.suite`, `enzo-core` v1). Each task is
   what a user would ask, what it starts with (files, memories), what the "user" answers to an
   approval, and its checks. Categories follow §13.2: tool accuracy, grounding, memory, safety,
   multi-step (§17's 3-tool task and max-steps task). A `FINGERPRINT` of the definitions is
   pinned by a test: a changed task without a new version fails CI, so results of different
   sets are never compared silently.
2. **Graded from the records, never by a model.** Checks read tool calls and arguments, approval
   records, workspace files, memory rows, scheduled jobs and the final reply. Reply checks are
   fixed patterns; grounding requires each price (within 1%) or date in the reply to appear in a
   live tool result. No LLM judge.
3. **The real loop and tools, on a throwaway everything.** `ConversationService` + `run_turn` +
   the gateway's own tool set (`enzo.agent.toolset.core_tools`, shared with the gateway). Each run
   creates a database beside the user's (same server, bundled or `ENZO_DATABASE_URL`), migrates
   it and drops it; each task starts from truncated tables, a temporary workspace and a fresh
   context; no runner starts, so nothing scheduled fires. No migration is needed.
4. **One brain at a time, pinned, no fallback.** The router used by a run sees only the brain
   under test, so every answer is its own (§15.1). A refused key stops that brain at once.
5. **Default brains:** each connected provider's everyday brain (its recommended one of the
   default tier, else the nearest stronger). `--brain NAME` (repeatable) or `--brain all`.
   Every connected brain by default would multiply cost and time by up to six for little gain.
6. **Per task:** status and reason, expected and actual, time to first word (from sending to the
   first word on screen), latency, tokens, cost (unknown stays `null`, plan-paid says so), the
   valid-first-try count, and the task's trace. Saved as JSON + Markdown (§15.5 columns) under
   the data folder (0700, files 0600), with the run's own log.
7. **Regression gate:** `enzo eval --compare A B` pairs the same brain (or the two brains of a
   swap), compares tasks graded in both, and exits 1 when the pass rate dropped (§13.2).
8. **Offline:** tasks needing the internet are skipped, not failed, when Tavily or example.com
   cannot be reached (or `--offline`); pass rates count graded tasks only.
9. **Time to first token** is measured by the router per call (`CallRecord.first_token_ms`,
   logged as `ttft_ms`). It is not stored in the ledger (no migration); traces read it from the log.
10. **`enzo trace [turn | last]`:** a task is one message and its runs (send + resumes), grouped
    by the same save rule as per-turn usage (`per_turn`). It joins the ledgers (model calls, tool
    calls, approvals by call id) with the log (route, stop, `ttft_ms`). Tool names and arguments
    are shown on purpose (§15.4); secrets and key shapes are redacted (`enzo.redact`) and control
    characters dropped. `--json` and `--full`.
11. **No secrets anywhere:** results redact the keys of every connected account and every
    key-shaped string (including OpenAI's masked echo of a rejected key).

## Found while building

- The max-steps report showed tool names and leaked the context note and recalled memory into
  the user's chat (found while writing the max-steps task's grader). Fixed in `agent/loop.py`.
- `enzo eval` and the gateway are two processes on one vault. ChatGPT refresh tokens rotate, so
  two renewals at once ended the sign-in. Renewal now holds a file lock and re-reads the vault.

## Not now

- Running tasks or brains in parallel (rate limits, comparable latency).
- An LLM judge for open-ended answers; if added, it must be marked as one in results.
- Storing time to first token in the ledger (needs a migration).
