# ADR 0021 — Reminders: delivered on time, in the chat, on the desktop, even with the app closed

- **Status:** Accepted (2026-11-25).
- **Spec:** §9.2 (persistent runtime: jobs survive restarts), §9.3 (scheduled work: owner, stop
  condition, delivery destination; verify it runs once, stops, and a cancelled job never runs).
- **Decisions it follows:** 2026-11-18 (background work runs while the computer is awake; missed
  jobs run on wake and say they are late) and 2026-11-20 (reminders before scheduled agent tasks).

## Decision

1. **What a reminder is:** words to tell the user at a time, once or repeating.
   - **Owner:** the user (Enzo has one).
   - **Destination:** the chat it was set in, as well as any open app and the desktop.
   - **Stop condition:** a one-time reminder stops after it fires. A repeat stops after `times`
     deliveries, after its `until` date, or when the user cancels it. "Until cancelled" counts as
     a stop condition here because a reminder only ever messages the user and costs nothing.
   - Agent runs on a schedule ("every morning, check X") are the next PR. They spend model calls
     and act, so they will need an approval and a hard stop (§9.3: "a yes to a one-time check
     does not authorize a recurring check").
2. **No model call to deliver.** The words are fixed when the reminder is set.
   - Reminders work with no account connected, never spend plan quota, and cannot fail on a
     provider.
   - They stay out of the transcript the model sees, so they never collide with a running reply,
     an open approval, or the tool-call pairing rules (#39).
   - A chat shows its delivered reminders in its timeline, at the point they arrived: each
     delivery records how many messages the chat had then.
3. **The user's own clock** (`enzo.schedule.recurrence`). The brain sets a local time; the zone
   comes from the user's app with the message, else this computer's own.
   - Repeats are counted in wall time: "every day at 9:00" stays at 9:00 across daylight-saving
     changes, and "monthly on the 31st" falls on the last day of shorter months.
   - Hourly repeats count real hours. A wall time skipped in spring comes out an hour later.
   - Repeats can be hourly, daily, weekdays, weekly or monthly, each every N.
4. **Exactly once.** Claiming due reminders records the delivery and moves the reminder on in
   one transaction, with `FOR UPDATE SKIP LOCKED`. Two gateways racing never deliver one twice.
5. **Late, not lost** (decision 2026-11-18). The runner checks the wall clock at least every
   30 s, and sleeps only until the next reminder when that is sooner, so it wakes on time and
   soon after the computer wakes from sleep.
   - A reminder due while Enzo was not running is delivered once, marked late ("due Mon 09:30,
     Enzo wasn't running"). Missed repeats in between are skipped.
   - After a reboot, missed reminders arrive the next time Enzo starts. Starting at login is left
     for `enzo deploy` and the login item.
6. **Delivery**, after the record is committed:
   - a desktop notification (macOS `osascript`, with the words passed as arguments and never as
     script; Linux `notify-send`; `ENZO_NOTIFY=0` turns it off);
   - a live event to every open app (`GET /events`, SSE with keep-alive), shown in the chat on
     screen. Apps reconnect with backoff;
   - the chat's timeline (`GET /conversations/{id}/reminders`).
7. **The gateway outlives the app while reminders are due** (§9.2).
   - When the last app closes and reminders are still to come, the launcher leaves the gateway on
     and says so ("Enzo stays on in the background for your 2 reminders (next: …). `enzo stop`
     turns it off.").
   - The gateway stops itself once none are left and no app is open. It does this only if it was
     left on for reminders: a background marker that the next app removes.
   - An app that opens while a gateway is still stopping waits for it, so two gateways never
     share one database.
   - `enzo stop` turns it off. `enzo doctor` shows the count and the next one.
8. **Tools** (hand-built, no approval; the chat shows the step "Setting a reminder: …"):
   `remind`, `list_reminders` and `cancel_reminder`. A time in the past is refused with the
   current local time. In the terminal, `/reminders` lists them and `/cancel` asks first (keep is
   the first choice).
9. **Logs** (ADR 0019): `reminder.set`, `reminder.cancelled`, `reminder.delivered` (with `late`),
   and `gateway.idle_stop`, each with numbers only, never the words.

## Not now

- Scheduled agent tasks (next PR), event hooks (§9.4) and goals (§6.4).
- Snooze, and editing a reminder in place: the user cancels it and asks again.
- The web app shows past reminders in a chat's timeline but does not listen live yet.
