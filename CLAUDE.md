# Enzo: instructions for Claude Code

Read this first in every session. It carries the context from earlier sessions.

## Source of truth

- `docs/spec/ENZO_SPEC.md` is the bible. Follow it literally. Never edit it (it must stay byte-identical).
- Architecture decisions live in `docs/adr/`. Record each new decision as an ADR.
- Before building a feature, re-read its spec section, including its "How to verify" steps.

## How Atishay wants to work

- **Communication:** short bullet points, plain language, lead with the answer. No walls of text. Never hide problems.
- **Verify, never assume:** check current versions and APIs against registries or the installed source, not memory.
  Test live, not just unit tests. When fixing a bug, show the regression test failing without the fix.
- **Modern and optimal:** before implementing, double-check it is how a strong engineer would build it today.
- **Blockers:** find a reasonable way around small ones. On a hard blocker, stop and ask. Fail loudly.

## Decisions from Atishay (2026-11-17)

- Memory rides inside the new user message, after the history (ADR 0007): cheaper, cache-friendly.
- Terminal app moves from Ink to OpenTUI (what OpenCode uses); pin the version, users need Bun.
- Bring your own account: Enzo ships no models. Users connect their own provider accounts (`/login`,
  `/models`, `/model`). ChatGPT via Sign in with ChatGPT (plan pays); Claude only via the user's own API
  key (Anthropic forbids app logins); Grok API key first, subscription login experimental; Kimi API key.
- Model catalog grouped fast / balanced / powerful with recommendations; defaults balanced (Claude
  Sonnet 5.5, GPT-6.2 Sol); smart routing per task, spread across the providers the user connected.

## Decisions from Atishay (2026-11-18)

- Enzo is **local-first and single-user**, like Claude Code / OpenCode but a personal agent: each person
  installs their own private copy, connects their own model accounts, grants permissions (files, browser,
  apps), and their data stays on their machine. No Enzo-run servers, no user accounts, no tenant separation.
- **One-line install, then `enzo`** (issue #23): everything bundled, nothing to pre-install (Postgres
  bundled, terminal app compiled, web app served by the gateway); `enzo web`, `enzo doctor`, `enzo update`.
- **24/7 later, still single-user:** `enzo deploy` runs the same private Enzo on an always-on machine the
  user owns (their cloud VM or a home machine). Until then, background work runs while the computer is
  awake; missed jobs run on wake and say they are late. A hosted service for everyone is not planned.
- Core tools hand-built; services via MCP. Order: GitHub connector (MCP), one-line install, then
  memory/background; **laptop use** (computer control with per-action approval) and a web agent after the core.

## Decisions from Atishay (2026-11-20)

- **Laptop use: approve once per task**, not every step. Enzo shows the task (what, which apps) once,
  then clicks and types on its own. It still pauses, with the exact action, before anything
  irreversible (send, buy, delete, post, enter a password or card; spec §12.3). A visible "Enzo is
  using your computer" sign and one key that stops it. macOS Screen Recording + Accessibility are
  granted once in System Settings; `enzo doctor` checks them.
- **Browser use is part of laptop use:** a real browser driven directly (its own profile; the
  user's logged-in one only with permission), for research, price comparison and checkout (the
  purchase waits for approval). Screen control is for desktop apps.
- Order after memory: reminders/scheduling + goals → Gmail/Calendar (MCP) → compaction (§5) →
  laptop + browser use → `enzo deploy`.

## Git and GitHub

- Branch from `main`: `feat/…`, `fix/…`, `chore/…`, `docs/…`.
- Commits: one line, Conventional Commits, e.g. `feat(brain): add provider fallback with single retry`.
- Every change lands through a PR that fills `.github/pull_request_template.md`. CI must be green.
- Merge method: rebase-merge. Delete the branch after merge.
- Standing permission from Atishay: merge your own PRs once CI is green.
- **Cadence (Atishay, 2026-11-18): slow down.** One PR per complete, user-visible feature, opened only
  when it is built, tested (unit, mutation, live) and documented. A few meaningful commits per PR (e.g.
  the bug fix, then the feature), each one building on its own. No docs-only, micro or follow-up PRs:
  docs and small fixes ride with the feature they belong to.
- Cloud sessions: GitHub GraphQL is blocked, so use `gh api` REST endpoints. They cannot create repos,
  delete branches, or approve PRs (GitHub never allows approving your own PR). The repo may be checked
  out as `Enzo` or `enzo`.

## Stack (ADR 0001)

- `server/`: Python 3.13, FastAPI, Pydantic v2, uv. ruff, mypy `--strict`, pytest with warnings as errors.
- `clients/sdk`: the only TS code that talks to the gateway. `clients/web`: React + Vite.
  `clients/cli`: OpenTUI 0.9.2 (pinned, pre-1.0) on Bun 1.4.4 (ADR 0008). `clients/chat`: shared chat state.
- Biome (lint/format, Markdown excluded), TypeScript 7, Vitest. pnpm workspace.
- Package `enzo-agent` on PyPI (`import enzo`, command `enzo`); migrations live in `server/src/enzo/migrations`.
- Postgres 16 + pgvector for persistence (ADR 0005); installed copies bundle it (`pixeltable-pgserver`, ADR 0016). No agent frameworks: brain adapters, loop, memory and
  safety are hand-built on official provider SDKs.

## Commands

```bash
cd server && uv sync && uv run ruff format --check . && uv run ruff check . && uv run mypy src tests && uv run pytest
```

```bash
pnpm install && pnpm check && pnpm typecheck && pnpm test && pnpm --filter @enzo/web build
```

One command runs everything from a clone (checks tools, installs, creates the DB, migrates, starts the
gateway, opens the app): `scripts/dev.sh` (`web`, `doctor`, `setup`; also `pnpm dev`). Keep it working.

Postgres 16 is required (tests use a real database, never mocks); pgvector is not used until memory
(spec §6). Local dev URL:
`postgresql+asyncpg://enzo:enzo@localhost:5432/enzo` (`ENZO_DATABASE_URL` overrides). Set it up with:

```bash
sudo apt-get install -y postgresql-16 && sudo pg_ctlcluster 16 main start
```

```bash
sudo -u postgres psql -c "CREATE ROLE enzo LOGIN SUPERUSER PASSWORD 'enzo'" && sudo -u postgres createdb -O enzo enzo
```

If a resumed session finds Postgres down: `pg_ctlcluster 16 main start`. Migrations: `uv run alembic upgrade head`.

Run pieces by hand: `cd server && uv run enzo-server` (port 8787), then `pnpm --filter @enzo/cli start` or
`pnpm --filter @enzo/web dev`. The Bun version lives in `.bun-version` (CI and the script read it).

## Gotchas already found

- Browsers throw "Illegal invocation" when `fetch` is called as a method of another object; the SDK wraps it.
- `EventSource` cannot POST: the SDK parses SSE from `fetch` (tested at every byte split).
- OpenTUI tests (`bun test`): render, type, wait and tear down inside React `act()` (helpers in
  `clients/cli/src/App.test.tsx`). Its `waitForFrame` does not let async work run: wait with real time.
- Real-terminal check (reads the screen through a terminal emulator), with a gateway running:
  `uv run --no-project --with pyte==0.8.2 python -I scripts/tty-smoke.py . "$(mktemp -d)"` in clients/cli.
- React StrictMode runs effects twice in dev: one-time startup work must be shared (see `connect()` in
  `clients/web/src/useChat.ts`), or it runs twice.
- Starlette's `TestClient` needs `httpx2`, not `httpx`.
- Biome formats Markdown by default; it is excluded so the spec stays verbatim.
- FastAPI has native SSE (`fastapi.sse`); no extra streaming library needed.
- Pydantic `model_validate_json(..., strict=True)` with `extra="forbid"` rejects coerced types and unknown
  fields: use it to validate tool-call arguments from the brain.
- Never name a module after a stdlib module (e.g. `types.py`): scripts run from that folder break.
- Every price in `brains.toml` carries its source and read date; an unconfirmed price is left out (cost unknown).
- Providers bill cache writes per retention (OpenAI 30m/12h, Anthropic 5m/1h): keep `cache_write_breakdown`.
- Tests: shared helpers live in normal modules (`tests/brain/schemas.py`), never imported from `conftest.py`.
- Prove a test has teeth: break the code on purpose, watch it fail, restore. Mind test order: a
  mutation can survive only because tests ran in a lucky order.
- Alembic `fileConfig` disables existing loggers unless `disable_existing_loggers=False` (kept in env.py).
- Mutations that make code hang need timeouts: pytest has `timeout = 30`; run mutation loops under `timeout`.
- Provider SDKs retry on their own (Anthropic: 2 retries). Adapters must set `max_retries=0` (ADR 0002).
- Current Claude models (Opus 5.5, Sonnet 5.5) reject forced `tool_choice` with a 400.
- Claude thinking blocks must be replayed unchanged: `model_dump(mode="json", exclude_unset=True)`.
- Cloud sessions: `api.anthropic.com` bypasses the egress proxy, so a key saved as a network secret is
  not injected there. Live Claude tests need another route.
- OpenAI: never use `responses.stream()` / `get_final_response()` in adapters: parsed wrappers add fields
  that break replay, and an "incomplete" (max tokens) stop raises. Read raw events (ADR 0002 #9).
- `curl` reaches `developers.openai.com`, `WebFetch` does not (different network path): fetch docs with
  `curl` (`/llms.txt` lists Markdown pages).
- FastAPI's 422 errors echo the submitted value (`input`): the gateway's handler drops it, so a bad
  request never sends a key back.
- Anthropic SDK 2.9 sends API keys as `Authorization: Bearer`, not `x-api-key` (check the installed source).
- Cloud sessions: the proxy blocks api.x.ai, api.moonshot.ai and api.openai.com (403). Key checks there
  report "could not check", which is correct; live tests need those hosts allowed.
- OpenTUI 0.9.2 has no password input: `clients/cli/src/KeyPrompt.tsx` reads keys via `useKeyboard` +
  `usePaste` into a ref and draws dots. Tests paste with `mockInput.pasteBracketedText`.
- GitGuardian flags realistic fake keys: tests use `tests/fakes.py` `fake_key()` (low entropy). Force-push
  is blocked here: to drop a flagged commit, push a new branch, open a new PR, close the old one.
- Routing never switches brains mid-turn: native reasoning parts are keyed by provider, not model,
  and replay across models is unverified (ADR 0010). The `done` event's `model` is the served model.
- `pnpm -s typecheck` hides failing packages' errors: run plain `pnpm typecheck` and read the output.
- Live streaming replies without real keys: a mock OpenAI-compatible server that streams
  `response.output_text.delta` then `response.completed` drives `OpenAIBrain` end to end.
- Provider keys come only from connected accounts. Environment keys are opt-in (`ENZO_USE_ENV_KEYS=1`) and
  then shown as method `environment` (#24): `build_brain`/`has_key` never read `os.environ` on their own.
- `done` events carry `failure` (e.g. `not_connected`) so each client words failures for its surface (#25).
- Tools that return outside content set `untrusted_output=True` (fenced by the registry); gated tools give a
  plain `summary` for the approval card. Never show tool names to users (§10.3).
- Tavily keyless search runs out fast under testing: tests mock it; connect a free key for real use.
- trafilatura warns about urllib3 SOCKS on import: `enzo.tools.web` silences exactly that warning.
- MCP tools: `enzo.connectors.mcp`. Approval unless the server marks `readOnlyHint`; outputs fenced; rate limit
  stops the connector 15 min, a rejected token until reconnect. Test servers: in-process `MCPServer`, and
  raise `mcp.server.mcpserver.exceptions.ToolError` (other exceptions reach clients as a generic message).
- GitHub MCP (checked live 2026-11-18): all 25 allowlisted tools exist; reads carry `readOnlyHint`;
  `pull_request_read` `get` returns REST-shaped JSON (`head.sha`), `get_diff` returns `{"diff": "..."}`.
- GitHub MCP is hosted (`api.githubcopilot.com/mcp/`, blocked in cloud sessions unless allowed). The cloud
  proxy only allows repo-scoped `api.github.com` paths (`/user`, `/versions` are refused here).
- Secrets: `enzo.accounts` vault (keychain or AES-GCM file with `ENZO_VAULT_KEY`), never plaintext (ADR 0009).
- Sign in with ChatGPT (OpenAI SIWC "ChatGPT plan usage", read 2026-11-18): public `api.openai.com/v1`
  Responses with the OAuth token, never `chatgpt.com/backend-api`. First sign-in `client_id=dynamic_agent_client`,
  then the issued id; `ext_agent_host_id` per install. Plan route: `store:false`, `stream:true`, no
  `max_output_tokens`, tools in a namespace. The callback is the gateway (`127.0.0.1:<port>/auth/callback`).
  Tests: `tests/accounts/fake_openai_auth.py` (real RSA-signed ID tokens; its transport yields, so races are real).
- OpenTUI wraps long lines mid-word: put a link a user must click or copy on its own line.
- Vite's dev server can keep serving an old `@enzo/chat` after edits: restart it with `--force` before
  judging a live web check.
- Brain HTTP uses `enzo.brain.transport` (TCP keepalive, 300 s read, 15 s connect; ADR 0015). The SDK
  defaults (600 s read) once locked a chat for 10 minutes (#46). Tests that fake a provider over HTTP
  monkeypatch `enzo.brain.factory.brain_http_client`.
- `asyncio.timeout` turns the cancellation into `TimeoutError` only when its block exits: catch it
  outside the `async with` (see `_stoppable` in `agent/loop.py`).
- Every tool call must be answered in the next message, or providers refuse the whole history (#39):
  `enzo.agent.history` closes calls the user moved on from and repairs older chats.
- An approval id covers the call id too (ADR 0014): a decision never applies to a later request.
- OpenTUI drops keys typed while the input is unfocused (an approval card or a dialog is up). The input
  stays focused while a reply streams; Enter then keeps the text instead of sending it.
- OpenTUI tests: the test renderer ignores flex `gap` (a real terminal draws it), so test content and order;
  check spacing live in a pty. `pressKey("up")` types the letters: use `pressArrow`, `"ESCAPE"`, `"RETURN"`,
  `"TAB"`. Shift+Enter needs `kittyKeyboard: true` (legacy terminals send a bare `\r`; OpenTUI asks real
  terminals for the kitty protocol by default). `testRender` quits on Ctrl+C unless `exitOnCtrlC: false`.
- Logs (`enzo.logs`, ADR 0019): JSON lines with the chat and `turn` from context variables; never
  message text. uvicorn needs `log_config=None` or it swaps our handlers out; its `color_message` holds
  escape codes. Our loggers get `propagate=False`: a test that configures logging must undo it.
- OpenTUI wraps at `-`, `/` and even no-break spaces: a long URL gets its own line (markdown
  `inlineHook`, `clients/cli/src/links.ts`), and a step's status and time are their own text.
- Keys can arrive faster than React redraws (fast typing, a macro): key handlers read the input's
  live `plainText`, never the last rendered state. Test it: type, then more keys + Enter in one `act()`.
- Memory (`enzo.memory`, ADR 0020): full-text search only (the bundled Postgres has pgvector but no
  `pg_trgm`). Tools read the chat and run from `enzo.context`. `mkstemp` files are already 0600.
- SQLAlchemy 2.1 deprecates `Result.tuples()` (warnings are errors here): iterate rows directly.
- Reminders (`enzo.schedule`, ADR 0021): the runner checks the wall clock every ≤30 s (timers stall while a
  Mac sleeps). httpx2's ASGI transport does not stream: test `/events` with a real uvicorn server.
  A gateway stops answering before its exit hooks finish: wait for the process, not for health.
- Scheduled tasks (ADR 0022): a run is `service.send(..., origin="schedule")` in the task's own chat; its
  framing rides in `TurnMeta.note` (context part), so clients show only the instruction. `job_var` tells
  `finish_task` which task is running. A task is claimed only while `running_since` is null.
- Bun's default 5 s test timeout kills a slow test but leaves it running, which breaks the tests after it:
  `clients/cli/bunfig.toml` sets 20 s. Shared terminal test helpers live in `clients/cli/src/testing.tsx`.
- React reuses a component at the same spot: dialogs get `key={kind}`, or a typed filter carries over.
- Tree-sitter code highlighting fails inside `bun test` (it warns and falls back) but works from source and
  in compiled binaries.
- Step words come from `Tool.activity` (then the approval `summary`, then a generic line), made printable
  and short by the registry: arguments come from the brain, so escape codes must never reach a terminal.
- Per-turn usage groups brain calls by save: a turn's messages share one transaction `now()`, and each
  call belongs to the first save after it started (`enzo.gateway.usage.per_turn`).
- A command card's title echoes arguments only when the command takes them (`echoOf`): a key pasted after
  `/login xai` must never be drawn.
- Tests share one database per session: list endpoints must be checked against the test's own ids.
- A fresh terminal chat has no id until its first message (`useChat` `start: "fresh"`): code that waits for
  "connected" must use the status, not `conversationId`.
- Uvicorn re-raises SIGTERM after shutting down: without a handler that exits normally, atexit hooks (the
  bundled Postgres stop) never run (`enzo serve`). The launcher also catches SIGHUP/SIGTERM to release.
- A child process that exited stays a zombie until reaped and still "exists" to `os.kill(pid, 0)`: use
  psutil's status and `wait()` your own children (`enzo.launcher`).
- uv_build puts files in the wheel even when git ignores them (`webapp/`, `_release.json` at release).
- Background jobs from a non-interactive shell ignore SIGINT: test Ctrl+C in a pty, not with `&` + `pkill -INT`.
- Running as root, pgserver runs Postgres as a `pgserver` system user: root-only failures there are not
  user bugs. Creating test users is blocked in this sandbox; CI runners (non-root, Linux + macOS) cover it.
- Google MCP (`gmailmcp`/`calendarmcp.googleapis.com/mcp/v1`, checked 2026-11-25) answers `server/discover` with
  "Invalid params": use `Client(mode="legacy")`. Its 401/429 bodies are JSON-RPC results, which the SDK turns into
  "Server returned an error response", so `enzo.connectors.mcp` reads the HTTP status through an event hook.
- Google accepts any loopback port and path for a Desktop OAuth client. Tests fake Google over HTTP with its real
  tool schemas (`tests/connectors/fake_google.py`, `google_tools.json`).
- Evals: each task runs in a fresh `contextvars.Context()` (`service.send` sets the chat var in the caller). Changing
  a task means bumping `VERSION` and `FINGERPRINT` (the failing test prints it); the scripted agent is
  `tests/evals/agent.py`. The router is pinned per run (`evals.harness.pinned`): no fallback.
- `enzo eval` and the gateway share the vault: ChatGPT renewal holds `.chatgpt-renewal.lock` and re-reads inside it.
- Time to first token: `CallRecord.first_token_ms`, logged as `ttft_ms`, not in the ledger. `enzo.redact` scrubs keys.
  JSON escapes C0 control characters but not C1 (`\x9b`): strip both before printing (`tracing.printable`).
- Memory.md carries an "Enzo wrote this copy at …" comment: a missing line counts as removed only if its memory existed
  by then; without the comment nothing is removed. File tools refuse Memory.md and Goals.md by casefold name or samefile.
- Goal rules live in `enzo.goals.store`, not only in the prompt; `user_today(now)` uses the turn's zone. The OpenAI
  adapter sends tool errors as `Error: {json}`: a mock provider must strip that prefix.
- Compaction: the prompt is `build_view(transcript, latest compaction)`; never edit stored messages. Kept turns from
  before a summary lose their replay data (Claude's thinking prefix check). Tools + system prompt are ~6.6K tokens: a
  small `compact_at` in a test config must sit above that. Helpers: `tests/compaction/fakes.py` (`RuleBrain`, tiny
  windows; give services a short `system_prompt`). Stored texts: `data_dir()/context/<chat>/`, ids are content hashes.
- `scripts/dev.sh` is tested by `server/tests/test_dev_script.py`: fake `psql`, `uname`, `node` … on PATH set
  each state (Mac/Linux, role or database missing, old server). Keep its FIX lines covered there.
- Pin actions by commit: `git ls-remote https://github.com/<owner>/<repo> refs/tags/<tag>^{}` (the API only
  reaches this repo from cloud sessions).

## Brain providers and auth (verified 2026-11-17, details in ADR 0001)

- Claude: API key only. Anthropic forbids third-party apps from using Claude.ai subscription login.
- ChatGPT: official "Sign in with ChatGPT" open-source flow (user's plan pays), API key fallback.
- Grok: API key. Subscription OAuth only as an "experimental" adapter.
- Kimi K3 / K3.1: Moonshot API (OpenAI-compatible). Not self-hosted (~1.4 TB weights).
- SDKs checked: `anthropic` 2.9.0, `openai` 3.53.0.

## User testing on Atishay's laptop

- A Claude Code agent on his Mac follows `docs/handoff/laptop-test-agent.md`: it runs Enzo as a user and
  files GitHub issues labelled `user-testing` (bugs) and `idea`. Check those issues at the start of each
  session and fix them through normal PRs. Keep that doc current when features land.

## Status and next steps

- [x] Scaffold: gateway, SDK, web and terminal UIs, CI (PR #1)
- [ ] Spec §1 brain layer, one PR per slice:
  1. [x] Canonical types (`enzo.brain.models`), `BrainProvider` protocol + `tool_call()`, strict
     tool-argument validation, `ScriptedBrain` for tests
  2. [x] Anthropic adapter (`AnthropicBrain`, ADR 0002); live conformance pending an API key
  3. [x] OpenAI Responses adapter (`OpenAIBrain`); live conformance pending a key; plan mode for Sign in with ChatGPT (ADR 0013)
  4. [x] Kimi (Moonshot) and Grok (xAI) configured via `OpenAIBrain` + `base_url` (ADR 0002 #11); verify live
  5. [x] Router, single fallback, budget check, cost accounting (`enzo.brain.router`, `brains.toml`, ADR 0003)
  6. Live conformance (§1.4) on every configured brain: needs API keys
- [ ] Spec §7 tools: [x] typed registry (`enzo.tools`, `@tool`), validation before execution, errors as data,
  timeouts, parallel batches; [x] batch 1 core tools (ADR 0011): `web_search` (Tavily, keyless or key),
  `read_page` (own fetch + SSRF guard), workspace files (list/read/create/edit/delete with approval),
  untrusted-output fence, plain-language approval summaries; [x] batch 2 MCP connectors + GitHub (ADR 0012,
  `docs/connectors/github.md`): schema tools, prepare-before-approval (merge pinned to head + diff), §8.3 stops;
  [x] batch 3 Gmail + Calendar via Google's MCP servers (ADR 0026, `docs/connectors/google.md`): `/login google [read]`,
  exact approval cards, reply recipients pinned, `google_status`; OAuth client choice pending Atishay (built for
  bring-your-own); batch 4 [x] memory tools (ADR 0020), [x] reminders (ADR 0021), [x] scheduled agent tasks (ADR 0022)
- [x] Spec §4 agent loop (`enzo.agent`: `run_turn`, approvals, prompt order; ADR 0004)
- [x] Spec §11 persistence base: Postgres store for transcripts, brain/tool call ledgers, approvals (ADR 0005)
- [x] Gateway: conversations, SSE turns, approvals, brain choice, auth (`enzo.gateway`, `enzo.agent.service`, ADR 0006)
- [x] Web chat: streaming, approval cards with exact arguments, brain picker, history (`clients/web`);
  redesigned 2026-11-19: welcome + suggestions, New chat, Accounts side panel, light/dark, phone width
- [x] Terminal chat (`clients/cli`, OpenTUI on Bun), sharing chat state with web via `@enzo/chat`
- [ ] Bring your own account (decision 2026-11-17):
  1. [x] Server: vault, connections index, live key check, `/connections` endpoints (ADR 0009,
     `docs/ops/provider-accounts.md`)
  2. [x] Clients: SDK + `/login` (hidden prompt), `/logout`, `/connections`, `/check`; web Accounts panel
  3. Model catalog and routing (ADR 0010): [x] server: tiers in `brains.toml`, task → tier, spread by
     conversation hash over connected providers, same-tier cross-provider fallback, rules classifier,
     tier pins, `done` names the answering brain; [x] clients: `/models`, `/model` (command table in
     `clients/cli/src/commands.ts`), web Model menu by tier, each live reply names its model
  4. [x] Sign in with ChatGPT (ADR 0013, `/login chatgpt`, web "Continue with ChatGPT"); live flow
     pending on the laptop (cloud blocks auth.openai.com)
- [x] One-line install + release automation (#23, ADR 0016, `docs/ops/release.md`): `install.sh`, `enzo` launcher
  (bundled Postgres, verified terminal-app download, web app served by the gateway), tag → Release workflow →
  GitHub release + PyPI `enzo-agent` (trusted publishing). First release pending Atishay's PyPI publisher setup.
- [x] GitHub connector: issue #2 (live write flow on a sandbox repo still to verify with a real token)
- [x] Terminal redesign (ADR 0017): #35 layout, #34 slash popup + palette, #36 rendering, #40 steps, #44
  offline, #49 usage per reply and per chat (also for reopened chats), `exit`/`quit`/Ctrl+D. Images (#35's
  lower-priority part) still to do.
- [x] v0.1.0 released (Atishay pushes release tags: this session's git proxy refuses tag pushes).
- [x] Laptop retest fixes: #58 new lines (`\` + Enter), #55 typing during approvals, #42 one confirmation
  (PR #59); #57 + #38 fresh starts, `enzo -c`, `/chats` (Ctrl+R), `/rename`, `/delete` to trash (ADR 0018).
- [x] #48 one JSON log per step, rotated file + `enzo logs` (ADR 0019); #43 plan billing on failed calls;
  #56 first-start message, `enzo update` wording, fast `/new`, long links and step times.
- [x] Memory (spec §6.2, ADR 0020): `remember`/`forget`/`search_memory` tools, recall before each
  message, corrections replace, secrets refused, `/memory` + `/forget`, `~/Enzo/Memory.md`; #41 folder named.
- [x] Reminders (spec §9.2/§9.3, ADR 0021): `remind`/`list_reminders`/`cancel_reminder`, wall-clock
  repeats, delivered once (late after sleep), chat timeline + live `/events` + desktop notification,
  gateway stays on in the background while due, `enzo stop`; `/reminders`, `/cancel`.
- [x] Scheduled tasks (spec §9.3, ADR 0022): approved `schedule_task` (a repeat must end; watches have
  `stop_when` + `finish_task`), each run a turn in the task's own chat (`origin = schedule`), outcomes
  reported/quiet/finished/needs_approval/failed, stops after 3 failures, recovered after a restart;
  `/scheduled` (also `/reminders`, `/tasks`), `/events` `task`. Also §12.6 hard boundaries in the rules.
- [x] Memory.md hand edits read back (ADR 0023): changed/added/removed lines become the user's corrections before
  each message; secrets refused and told; malformed edits never lose data; both-changed rules in the ADR.
- [x] Goals (spec §6.4, ADR 0023): `create_goal` / `record_goal_progress` / `close_goal` / `list_goals`, evidence for
  every change, a step never closes a goal, closing cancels linked check-ins (`goal` on remind/schedule_task), open
  goals in each message, read-only `~/Enzo/Goals.md`, `/goals [done]`. Goal tools are in `core_tools` (evals see them).
- [x] Compaction (spec §5, §1.3.5, ADR 0024): local-first token counting (exact count near a limit), per-brain
  budgets from sourced windows in `brains.toml [context]`, summaries of older turns by the chat's own provider
  (structured record, corrections verbatim, invariants in code, migration 0010), long tool results and pastes kept
  as files + `read_stored_output`, a "summarized" line in the chat. Real-model check: `scripts/compaction-check.py`.
- [x] Evals + traces (spec §13, §15.4/§15.5, ADR 0025, `docs/evals.md`): `enzo eval` runs 20 fixed tasks
  (`enzo.evals.suite` v1, `FINGERPRINT` pinned) through the real loop and tools on each connected account's
  everyday brain (`--brain`, `--brain all`, `--task`, `--offline`), in a throwaway DB + workspace, graded from the
  records; `--compare A B` exits 1 on a pass-rate drop; `enzo trace [turn|last]`. Real-brain run pending on the laptop.
- Then continue in the order of the spec §17 checklist.
