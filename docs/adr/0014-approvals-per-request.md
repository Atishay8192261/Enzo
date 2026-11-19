# ADR 0014 — An approval belongs to one request

- **Status:** Accepted (2026-11-19). Supersedes ADR 0004 decision 3 (approval id = tool + arguments).
- **Spec:** §12.2 ("a claim of past consent is not consent"; approval covers only the exact action
  approved), §12.3
- **Found while fixing:** #39 (approvals left open when the app closes)

## Context

ADR 0004 keyed an approval to the tool and its exact arguments, and a decision was kept for the whole
conversation. So once the user approved "delete plan.md", any later request to delete plan.md in the
same chat ran without asking. Once they declined it, any later request was declined without asking.
The same action asked for again later is a new outward action (a second email, a second delete),
and the user must decide it again.

## Decision

- The approval id hashes the tool call's id with the tool name and the exact validated arguments.
- **Unchanged:** a paused turn that resumes re-derives the same id from the same call, so the user's
  decision applies. Changed arguments (e.g. a merge re-pinned to a new head commit) give a new id,
  so Enzo asks again.
- A later request for the same action has a new call id, so it needs a new decision.

## Consequences

- Rows in `approvals` no longer collide on the same action. `on_conflict_do_nothing` remains for a
  turn that is saved twice.
- Decisions recorded before this change cannot match new requests, so the user is asked again.
