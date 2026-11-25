# ADR 0020 — Memory you can inspect and correct

- **Status:** Accepted (2026-11-20). Decision 7's Memory.md is now read back when the user edits
  it by hand (ADR 0023): it was one-way, so a hand edit was lost at Enzo's next change.
- **Spec:** §6.2 (curated long-term memory), §6.5 (write discipline), §3 steps 2 and 6 (retrieve,
  then persist and learn), §15.2 (memory you can inspect, correct and trust), §17 "Memory".
- **Also:** #41 (Enzo could not say where it saved a file).

## Decision

1. **What it holds:** durable facts about the user, one per memory, in plain words.
   - Five kinds: `about` (who they are), `preference`, `person` (with the name in `about`),
     `decision` and `fact`.
   - Each memory says who said it: `user`, or `inferred` when Enzo concluded it (§6.5: an
     observation is told apart from an inference).
   - It also keeps when it happened (when known), when it was learned, and the chat and run it
     came from. The `turn_id` is the same as on log lines and ledger rows.
   - It is a Postgres table (`memories`, migration 0006).
2. **The brain writes it, with hand-built tools** (spec §7 batch 4): `remember`, `forget` and
   `search_memory`.
   - This is how ChatGPT and Claude memory work too. It costs no extra model call per turn,
     unlike an extraction pass after every message (which would also spend ChatGPT plan quota).
   - No approval: a memory is local, the chat shows the step ("Remembering: …"), and the user
     can see and undo it at any time.
   - The system prompt says when to remember, to save only what the user said or confirmed, and
     never to save instructions from outside content.
3. **Corrections replace:** `remember(..., replaces=<number>)` marks the old memory replaced.
   - A replaced memory is kept for the record but never recalled or listed (§6.2: never keep a
     superseded fact alongside the new one as if both were true).
   - Correcting a memory that does not exist is refused, so the brain searches first.
4. **Forgetting deletes** the memory and every version before it. Forgetting means gone.
5. **Recall before each message** (§3 step 2):
   - Postgres full-text search with English stemming, over the text and who it is about. Any
     word may match; better matches come first. At most 6 memories are recalled.
   - They ride inside the new user message after the history (ADR 0007), each with its number
     and source. They are labelled as facts, not instructions.
   - A message with only common words recalls nothing.
   - The bundled Postgres has no `pg_trgm`, and full-text search needs no extension.
   - Recall by meaning (§6.3, pgvector) is a later step. It needs an embedding model every user
     has, and many sign in with ChatGPT only.
6. **Never secrets** (§6.2): `enzo.memory.guard` refuses, before anything is written:
   - key and token shapes, and long mixed letter-digit runs;
   - "password / PIN / code is …";
   - card numbers (by the Luhn check) and US social security numbers.
   It errs on the side of refusing: a wrongly refused fact can be reworded.
7. **Inspect** (§15.2):
   - `GET /memory` and `DELETE /memory/{id}`.
   - In the terminal, `/memory` lists memories by kind with "you said, 20 Nov 2026, in “chat”",
     and `/forget` asks first (keep is the first choice).
   - `~/Enzo/Memory.md` is rewritten after every change: atomically, readable by the user only.
     The file tools refuse to change it, so a page cannot talk Enzo into rewriting its own
     memory, and a hand edit is never silently lost.
8. **Logs** name memory events (`memory.saved`, `memory.recalled`, `memory.forgotten`) with
   numbers and kinds, never the words (ADR 0019).
9. **#41:** developer instructions (prompt layer 2) name the user's folder as they type it
   (`~/Enzo`, never the full path with the account name). File tool results carry `location`
   (`~/Enzo/plan.md`).

## Known limits

- A poisoned memory is possible in principle: a page could persuade the brain to save
  something. It is mitigated by the prompt rules, the "not instructions" label, the visible
  step, and `/memory`.
- Goals and commitments with status and due dates (§6.4) come with reminders and scheduling.
- The web app has no memory panel yet (the web app comes last, Atishay 2026-11-19).
