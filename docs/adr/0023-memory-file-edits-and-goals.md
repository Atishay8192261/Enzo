# ADR 0023 — Memory.md edited by hand is read back; goals as records with evidence

- **Status:** Accepted (2026-11-25).
- **Spec:** §11.2 ("the structured store and the human-readable files must be reconciled; they
  must not drift into two different truths"), §11.3 ("edit a memory file by hand and confirm Enzo
  uses the edited value on the next question"; "create a goal, restart Enzo, and confirm the goal
  is still there with the same status"), §6.2, §6.4 (structured goals), §6.5 (write
  discipline), §9.2, §15.2.
- **Amends:** ADR 0020 decision 7, where `~/Enzo/Memory.md` was written from the table only.

## Context

`Memory.md` was one-way: a hand edit was never read, and Enzo's next change of memory wrote the
file again over it. The brain kept using the old value. That is two truths, and a silent loss of
the user's words.

## Decision: Memory.md is two-way

1. **When it is read.** Before each message's recall (spec §3 step 2), before Enzo writes the
   file again (`remember`, `forget`), and before `/memory` lists or the brain searches. The check
   is cheap: the file's size, modification time and inode are compared with what Enzo last wrote
   or read; the file is opened only when one differs. Reading and writing share one lock.
2. **What a hand edit means.** Each memory is one `- ` line under its kind's heading, ending
   with its source and number (`_(you said, 20 Nov 2026; #3)_`).
   - A **changed** line (text, who it is about, or the heading it is under) is a correction: a new
     memory replaces the old one (§6.2: the old one is kept as replaced, never recalled).
   - An **added** line is a new memory, of the kind of the heading it is under.
   - A **removed** line is forgotten: deleted with its versions, as with `/forget` (ADR 0020:
     forgetting means gone).
   - Each is the user's own words: `said_by = user`, and its source reads "edited by hand in
     Memory.md, <date>" (in `/memory`, the file, and what the brain is told).
   - A line that lost its number but still reads the same is the same memory, not a new one; a
     line twice is saved once.
3. **Secrets are still refused** (§6.2, `enzo.memory.guard`). A refused line is not saved (a
   changed one leaves its memory as it was) and taken out of the file. The user is told plainly,
   twice: a dated note in the file's "Not saved" section, and a note with their next message so
   the brain says what was not saved and why. A note never repeats the refused words.
4. **A malformed edit never loses data.**
   - Lines Enzo cannot read as memories (prose, a quote) are kept under "## Not saved", word for
     word, until the user moves or deletes them (never a secret: see 3).
   - Without the line that says which copy it is (the top was cut, the file was emptied),
     nothing is removed, and the user is told how to remove one.
   - A deleted file is written again; nothing is forgotten.
   - A file that is not UTF-8 text is set aside as "Memory (unreadable copy <time>).md" before
     Enzo writes `Memory.md` again.
   - If reading fails for any other reason, Enzo logs it and does not write the file until a
     read succeeds, so the edit waits instead of being written over.
   - Every change from one edit is applied in one transaction: all of it or none.
5. **What wins when the file and the table both changed** (an editor kept an older copy open
   while Enzo learned something in chat). The file names when Enzo wrote that copy (an HTML
   comment near the top), and memories keep their numbers, so Enzo compares three versions:
   - A line the user did **not** change never undoes a change made in chat: an old copy of a
     corrected memory leaves the correction in place, and a forgotten memory stays forgotten.
   - A memory learned after that copy was written is not "removed" by its absence.
   - A line the user **did** change, for a memory also corrected in chat since: the newer change
     wins, by the file's save time against the correction's time (§6.2: use the latest
     decision). A losing edit is noted in the file, not dropped silently.
   - A changed line for a memory forgotten in chat is not saved (forgetting wins), with a note
     on how to add it again.
6. **Only the user edits it.** The file tools refuse `Memory.md` by any spelling (`memory.md` is
   the same file on macOS) or link, because hand edits count as the user's words and a web page
   could talk the brain into writing them.
7. **Where "edited by hand" is kept.** A memory written from the file has `turn_id = "Memory.md"`
   instead of a run's id: no new column, so this fix needs no migration of its own.
8. **Logs:** `memory.file_edits` with counts only; `memory.file_unread` when reading failed.

## Consequences

- Spec §11.3's check passes: edit the file, and the next message carries the edited value.
- The file gained a comment line near the top and a short "how to edit" introduction. Older
  files (without the comment) are read safely: nothing in them can make Enzo forget.
- One gateway per database (ADR 0021) keeps the lock enough; two gateways never share one.

## Decision: goals (spec §6.4, tier 4)

1. **A goal is a record** (`goals`, `goal_steps`, `goal_events`; migration 0009): a number, a
   title, a status (`open`, `done`, `dropped`), a due date if any (the user's local date), the
   place in the user's Enzo folder where its work lives (never outside it), and its steps in
   order: what remains. Where it was set (chat and run) is kept, as for memories.
2. **Every change keeps its evidence** (§6.5), in `goal_events`: what shows it (in words), how it
   is known (`user_confirmed`: the user said or confirmed it; `verified`: a tool result shows
   it; `timeline_ended`: its due date has passed), a source to reach it again (needed when
   verified; the chat and run are always kept), and when it happened if known. An inference is
   not a basis: the tools do not accept one, and the rules say to ask the user instead.
3. **The rules live in the store, not only in the prompt**, so every caller obeys them:
   - a step is done, or a goal closed, only with evidence and a basis; a step never by a date;
   - evidence that reads as a plan or an offer ("will", "going to", "proposed") is refused: a
     proposed action is never recorded as done (§6.5). It errs on refusing; the brain rewords;
   - recording steps never closes a goal, even the last one (the result says the goal stays
     open, or that the outcome itself now needs confirming);
   - closing a goal as done while steps remain needs the user's own word that the whole goal is
     done: a verified step or a passed date cannot do it (§6.4 "a single successful step does
     not close a goal");
   - `timeline_ended` is checked against the user's own date: a goal not yet due is refused;
   - titles, steps, evidence and sources pass the memory guard (never secrets); two open goals
     with the same title are refused.
4. **Enzo closes goals without being asked**: open goals ride with every message (below), the
   rules say to close one as soon as the evidence shows it is done, and the progress tool's
   result says when every step is done that the outcome itself is what to confirm next.
5. **Open goals ride with each message** in the memory part of the new user message, after the
   history (ADR 0007, cache-friendly): at most 10 (due soonest first), each with its due date
   (marked when passed), folder, check-ins, steps done (with their basis) and steps left (at
   most 8 shown). Labelled as the user's commitments, not instructions.
6. **Stops reminding** (§6.4): a reminder or scheduled task may check in on a goal (`goal` on
   `remind` and `schedule_task`; `jobs.goal_id`). Closing or dropping the goal cancels every
   check-in still to come in the same transaction as the close, and the result names them. A
   check-in can only be set for an open goal.
7. **Tools** (no approval: a goal is a local record, each change shows as a step): `create_goal`,
   `record_goal_progress` (steps done with evidence; steps to add), `close_goal` (done or dropped,
   with evidence) and `list_goals`. Endpoint `GET /goals?which=open|closed`.
8. **Goals.md is a read-only copy**, not two-way like Memory.md, because every change of a goal
   needs its evidence and basis, which a hand edit cannot carry, and a ticked box would bypass
   the rule that one step never closes a goal. §11.2 asks for editable files for curated memory
   (tier 2); goals are tier-4 records. So:
   - it is written with mode 0400 (an editor warns before saving over it) and says how to change
     a goal (tell Enzo, or `/goals`);
   - it ends with a check of its own words. If it was edited anyway, Enzo keeps that copy as
     "Goals (your edit <time>).md", writes the file again from its records, and tells the brain,
     which asks the user what they wanted to change: never a silent loss, never a second truth;
   - the file tools refuse it, by any spelling, like Memory.md.
9. **Restarts** (§9.2, §11.3): goals live in Postgres; nothing about them is held only in memory.
10. **Logs:** `goal.created`, `goal.progress`, `goal.closed` (outcome, basis, stopped check-ins)
    and `goals.file_edited`, with numbers only, never the words.

## Not now

- Reopening a closed goal (the user asks Enzo to track it again: a new goal), deleting a goal,
  and editing a step's words.
- People and relationships as records (the rest of tier 4).
- The web app shows no goals yet (the web app comes last, Atishay 2026-11-19).
