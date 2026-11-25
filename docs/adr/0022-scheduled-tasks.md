# ADR 0022: Scheduled tasks, Enzo working on its own at a time and reporting back

- **Status:** Accepted (2026-11-25).
- **Spec:** §9.3 (scheduled work: owner, stop condition, delivery destination; each run finishes; scope
  is exactly what the user approved), §9.2 (jobs survive restarts), §12.4 (a page must never be able to
  create "a new scheduled job … the user did not authorise"), §12.6 (hard boundaries, added to the rules
  in the first commit of this PR).
- **Builds on:** ADR 0021 (reminders: the runner, wall-clock repeats, `/events`, desktop
  notifications, the gateway staying on in the background).

## Decision

1. **What a task is:** an instruction Enzo carries out at a time, once or repeating. Examples: "every
   morning at 8, check the weather and tell me", "tell me when a table for two is free on Friday".
   Each run is a full agent turn with tools. It is stored as a job of kind `task`, next to reminders, in
   the same table and with the same recurrence.
2. **Approval of the exact plan.** `schedule_task` is gated like any action that acts on its own. Each
   run spends the user's model quota, and §12.4 forbids outside content from starting one. The card
   shows the plan in words ("Schedule a task, every weekday at 08:00, until 1 Dec 2026: …") and the
   exact arguments. Reminders stay ungated: they only ever message the user.
3. **A hard stop.** A repeating task must end, on a date (`until`) or after a number of runs (`times`).
   Without one it is refused, and the brain is told to ask the user (§9.3: a yes to one check does not
   authorise a recurring one). A watch also has `stop_when` ("a table for two is free on Friday"): the
   run that finds it calls `finish_task` (it only works inside a scheduled run), reports, and the task
   stops. A task also stops after `MAX_FAILURES` = 3 failed runs in a row. A success resets the count.
4. **Runs happen in the task's own chat**, titled "⏰ <instruction>" and made at the first run. A run
   never collides with the user's own reply in progress, an open approval, or the tool-call pairing
   rules (#39). The run's message is the instruction itself, saved with `origin = "schedule"`, so the
   clients show it as "⏰ Scheduled run: …", not as something the user typed. The brain also sees a
   note in the context part of that message: task number, run n of at most N, who set it and when, the
   stop condition, and a late note when it was due while Enzo was off.
5. **Outcomes** of each run are recorded in `job_runs.outcome`:
   - `reported`: the run answered;
   - `quiet`: a watch whose condition is not met yet. No notification, no pop-up, and nothing in the
     originating chat. The run's words stay in the task's own chat;
   - `finished`: the stop condition was met;
   - `needs_approval`: the run reached a gated step. The task waits in its own chat, and nothing is
     sent or changed without the user (§12.3);
   - `failed`: the brain failed, refused, ran out of steps or budget, or the run broke.
6. **Delivery**, as for reminders: a one-line report ("<what>: <first line of the answer>", plus "Done:
   it stops now.", "That was its last run." or "It stopped after failing several times.") goes to the
   originating chat's timeline, to every open app (`/events`, event `task`), and as a desktop
   notification. Quiet runs are skipped everywhere except the task's own chat.
7. **One run at a time, recovered after a restart.** Claiming a due task sets `running_since` in the same
   transaction that moves it on. A task with a run still going is not claimed again. If the user's own
   turn in the task's chat is running, the run waits and retries (up to 10 times, a minute apart). After a
   restart, a run that was going when Enzo stopped is recorded as failed (`task.interrupted`), and the
   task carries on at its next time. While a run is going the gateway never stops itself for being idle.
8. **One list for both kinds.** `GET /scheduled` and `DELETE /scheduled/{id}` replace `/reminders`.
   `GET /conversations/{id}/scheduled` gives a chat's timeline notices. The brain's tools are `remind`,
   `schedule_task`, `finish_task`, `list_scheduled` and `cancel_scheduled`. In the terminal,
   `/scheduled` (also `/reminders` and `/tasks`) lists reminders and tasks in two sections, and `/cancel`
   asks first. The launcher and `enzo doctor` count both ("for your 2 scheduled items").
9. **Logs** (ADR 0019): `task.set`, `task.start` (with the run number), `task.run` (outcome, stopped and
   the ledger's `run_id`), `task.cancelled`, `task.interrupted` and `task.run_failed`. They carry
   numbers only, never the instruction or the report.

## Why not

- **Running in the originating chat:** the run would close the user's open tool calls and could land
  in the middle of the user's own reply.
- **No approval for tasks:** a web page could plant "every hour, email this to …".
- **"Until cancelled" for tasks:** every run costs model quota. A forgotten task would spend it for good.

## Not now

- Event hooks (§9.4): polling a condition outside a model run, with a dry run before enabling.
- Editing a task in place: the user cancels it and asks again.
- The web app shows past reports in a chat's timeline but does not listen live yet.
