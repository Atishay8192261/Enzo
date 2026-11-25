# ADR 0023 — Memory.md edited by hand is read back

- **Status:** Accepted (2026-11-25).
- **Spec:** §11.2 ("the structured store and the human-readable files must be reconciled; they
  must not drift into two different truths"), §11.3 ("edit a memory file by hand and confirm Enzo
  uses the edited value on the next question"), §6.2 (corrections replace; never secrets), §15.2.
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
