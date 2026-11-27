# ADR 0031 — Watches: Enzo checks for a change without a model call, and wakes once per change

- **Status:** Accepted (2026-11-27).
- **Spec:** §9.4 (event-driven work: hooks start disabled, a dry run is inspected before enabling,
  no messages for routine unchanged results; verify it fires once and stays silent), §9.2, §9.3
  (owner, stop condition, delivery destination), §8.3 (rate limits), §12.3–12.4, §13.
- **Builds on:** ADR 0021/0022 (the runner, `/events`, desktop notifications, background stay-on,
  runs in their own chat), 0011 (`read_page`'s fetch and SSRF guard), 0012/0026 (MCP connectors).

## Decision

1. **What a watch is** (`enzo.hooks`; the spec says hook, people see "watch"): a cheap check on an
   interval, with no model call, that extracts a small normalized value. Only a change by its rule
   starts an agent turn, in the watch's own chat ("👀 <what>"), told the readings before and after.
   - Sources: a page (the first element matching a CSS selector, the first main-text line holding
     some words, or the whole main text), new Gmail emails matching a search, new or moved calendar
     events, a GitHub pull request's checks (one state: running, passing, failing) or new comments.
   - Rules: `changes`, `new` (an item appeared), `above`/`below` (the number crossed the
     threshold: staying past it never wakes again), `appears` (words arrived).
   - Long readings are compared by digest; layout and whitespace alone are never a change.
2. **Two yeses** (§9.4, §12.4). Setting one up is approval-gated: Enzo reads it once before
   asking, the card shows what it reads now, and a check that fails asks nothing. It is saved
   **off**. Turning it on is a second gated step (`enable_watch`, `/enable`, `POST /watches/{id}/enable`):
   a fresh dry run, recorded, shown on the card with what counts as a change. The yes names that
   dry run by a token over the reading, so re-checking just before acting keeps the yes when the
   reading is the same and asks again when it changed (§12.3). The watch starts from that reading.
3. **One wake per change, across restarts.** A check that sees a change saves the new reading and
   a pending wake in one locked transaction; the next check compares with the saved reading. Wakes
   run one at a time per watch; one interrupted by a restart is recorded as failed, never re-run.
   Checks are claimed with `polling_since` (`FOR UPDATE SKIP LOCKED`), so none runs twice.
4. **Stops:** an end is required (`until` or `times` wakes; "tell me when" is one). Failed checks
   back off (interval × 2ⁿ, at most 6 h) and stop after 5 in a row. A rate limit (HTTP 429 from the
   site, or the connector's own stop) stops every watch of that scope (the site, or Gmail/Calendar/
   GitHub) at once with no retry; a refused or missing sign-in stops it too. Each stop is reported
   in the chat it was set in; `/enable` turns a stopped one on again after a new dry run.
5. **Local pages:** a page on this computer or the LAN is read only when the card said so (`local`,
   computed before asking, never taken from the brain) and only that exact site; redirects elsewhere
   get the normal SSRF guard; Enzo's own gateway port is never read.
6. **Silence and delivery:** unchanged checks only log numbers. A wake's report and a stop go to
   the originating chat's timeline (`/conversations/{id}/scheduled`, kind `watch`), to open apps
   (`/events`, event `watch`) and to the desktop. Readings reach the brain fenced as outside data.
7. **Background:** the reminders runner drives watches as a worker on the same wall clock; the
   gateway stays on while a watch is on (`launcher.watching`), and `enzo doctor` counts them.
8. **Logs:** `hook.set`, `hook.dry_run`, `hook.enabled`, `hook.poll` (ms, ok, changed, failures),
   `hook.changed`, `hook.wake`, `hook.stopped` (reason, polls, wakes), numbers only.

## Limits and not now

- The minimum interval is one minute. Pages that need JavaScript are not read (use the browser).
- Gmail/Calendar/GitHub answer shapes are from Google's tool descriptions and GitHub's MCP source
  (read 2026-11-26), not from live accounts: a changed shape fails closed (the check fails).
- Calendar: a deleted or cancelled event that the server stops listing is not a change; moved and
  new ones are. Pull requests with more than 500 comments: later ones are not seen.
- Editing a watch in place: cancel it and ask again. The web app shows reports in the timeline only.
