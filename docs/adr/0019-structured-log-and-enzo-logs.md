# ADR 0019 — One structured log, tied to the ledgers, that `enzo logs` reads back

- **Status:** Accepted (2026-11-20).
- **Issues:** #48 (the gateway log held only HTTP lines, and an installed Enzo wrote no log file
  at all), #43 (failed ChatGPT-plan calls billed as unknown usage; tool rows all stamped with the
  save time), #56 (a silent first start).
- **Spec:** §1.4 ("a log line for a single task that names the provider, model, input tokens,
  output tokens, and latency"), §11.3 (no secrets or message text in logs).

## Decision

1. **JSON lines, one per step** (`enzo.logs`):
   - Each line has `ts`, `level` and `event`, plus the step's own fields.
   - The steps: `turn.start`, `route`, `brain.call` (provider, model, tokens, ms, billing, cost),
     `tool.call` (tool, status, ms), `approval.wait`, `turn.end` (stop, steps, calls, ms) and
     `turn.failed` (with the traceback).
   - Start-up writes `database.ready` (with `first_start` and ms) and `gateway.start`.
   - HTTP requests become `http` lines: method, path and status. The query string is never
     logged (it holds the one-time sign-in code). Health checks that answered OK are left out.
2. **Correlated without plumbing:**
   - Context variables hold the conversation and the run (`turn`, `t_<12 hex>`). They are set
     once per send or resume, so every line inside the run carries them.
   - The same `turn_id` is stored on `brain_calls` and `tool_calls` (migration 0005). The log
     and the database join on one column.
3. **What lines never hold:** message text, tool arguments, tool results, keys or tokens. A test
   sends marked words through a turn and checks that none reach the log. `ENZO_LOG_CONTENT`
   (suggested in #48) is not added: nothing logs content, so there is nothing to switch on.
4. **Where it goes:**
   - An installed Enzo writes `~/.local/state/enzo/logs/gateway.log` (`$XDG_STATE_HOME`,
     `ENZO_STATE_DIR`). It is rotated at 5 MB, with 3 old files kept.
   - The folder is 0700 and every file 0600, rotated ones included.
   - `enzo serve` also writes the lines to stderr. A developer's `enzo-server` writes them to
     stderr only, which `scripts/dev.sh` keeps in `.dev/gateway.log`.
   - uvicorn runs with `log_config=None`, so its lines go through the same handlers. Its
     `color_message` copy (escape codes) is dropped.
   - What the gateway printed before its log was set up (a crash at start) goes to
     `logs/startup.log`, kept for the latest start only. When a start fails, the launcher shows
     its end.
5. **`enzo logs`:**
   - Shows the last lines readably: local time, a mark for warnings and errors, the event and its
     fields, with the conversation cut to 8 characters.
   - `-n N` sets how many lines. `-f` keeps following, across rotations. `--json` prints the
     lines as written, to attach to a bug report. `--path` prints only where the file is.
   - Escape codes are dropped from what it prints: lines carry error text from outside.
   - `enzo doctor` says where the log is.
6. **Who pays is known before the call (#43).**
   - The router reads the brain's `billing` ("plan" for a ChatGPT sign-in) when it picks the
     brain. A failed or stopped call on the plan is the plan's, never per-token usage of unknown
     cost.
   - Migration 0005 repairs older rows: a failed call by a brain that the same chat used on the
     plan becomes the plan's.
   - `done` names the brain that failed when none answered.
   - A tool call's row keeps when the call started.

## Not now

- No OpenTelemetry, Datadog or ELK. Enzo is a single-user local app, and its ledgers are already
  in Postgres. Revisit only if Enzo ever runs as a hosted service.
- The `trace` timeline command from #48 waits. The log plus `turn_id` on the ledger rows already
  answer "what happened in this turn".
