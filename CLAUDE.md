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

## Git and GitHub

- Branch from `main`: `feat/…`, `fix/…`, `chore/…`, `docs/…`.
- Commits: one line, Conventional Commits, e.g. `feat(brain): add provider fallback with single retry`.
- Every change lands through a PR that fills `.github/pull_request_template.md`. CI must be green.
- Merge method: rebase-merge. Delete the branch after merge.
- Standing permission from Atishay: merge your own PRs once CI is green.
- Cloud sessions: GitHub GraphQL is blocked, so use `gh api` REST endpoints. They cannot create repos,
  delete branches, or approve PRs (GitHub never allows approving your own PR). The repo may be checked
  out as `Enzo` or `enzo`.

## Stack (ADR 0001)

- `server/`: Python 3.13, FastAPI, Pydantic v2, uv. ruff, mypy `--strict`, pytest with warnings as errors.
- `clients/sdk`: the only TS code that talks to the gateway. `clients/web`: React + Vite.
  `clients/cli`: OpenTUI 0.9.2 (pinned, pre-1.0) on Bun 1.4.4 (ADR 0008). `clients/chat`: shared chat state.
- Biome (lint/format, Markdown excluded), TypeScript 7, Vitest. pnpm workspace.
- Postgres 16 + pgvector for persistence (ADR 0005). No agent frameworks: brain adapters, loop, memory and
  safety are hand-built on official provider SDKs.

## Commands

```bash
cd server && uv sync && uv run ruff format --check . && uv run ruff check . && uv run mypy src tests && uv run pytest
```

```bash
pnpm install && pnpm check && pnpm typecheck && pnpm test && pnpm --filter @enzo/web build
```

Postgres 16 + pgvector is required (tests use a real database, never mocks). Local dev URL:
`postgresql+asyncpg://enzo:enzo@localhost:5432/enzo` (`ENZO_DATABASE_URL` overrides). Set it up with:

```bash
sudo apt-get install -y postgresql-16 postgresql-16-pgvector && sudo pg_ctlcluster 16 main start
```

```bash
sudo -u postgres psql -c "CREATE ROLE enzo LOGIN SUPERUSER PASSWORD 'enzo'" && sudo -u postgres createdb -O enzo enzo
```

If a resumed session finds Postgres down: `pg_ctlcluster 16 main start`. Migrations: `uv run alembic upgrade head`.

Run locally: `cd server && uv run enzo-server` (port 8787), then `pnpm --filter @enzo/cli start` or
`pnpm --filter @enzo/web dev`.

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
- Secrets: `enzo.accounts` vault (keychain or AES-GCM file with `ENZO_VAULT_KEY`), never plaintext (ADR 0009).

## Brain providers and auth (verified 2026-11-17, details in ADR 0001)

- Claude: API key only. Anthropic forbids third-party apps from using Claude.ai subscription login.
- ChatGPT: official "Sign in with ChatGPT" open-source flow (user's plan pays), API key fallback.
- Grok: API key. Subscription OAuth only as an "experimental" adapter.
- Kimi K3 / K3.1: Moonshot API (OpenAI-compatible). Not self-hosted (~1.4 TB weights).
- SDKs checked: `anthropic` 2.9.0, `openai` 3.53.0.

## Status and next steps

- [x] Scaffold: gateway, SDK, web and terminal UIs, CI (PR #1)
- [ ] Spec §1 brain layer, one PR per slice:
  1. [x] Canonical types (`enzo.brain.models`), `BrainProvider` protocol + `tool_call()`, strict
     tool-argument validation, `ScriptedBrain` for tests
  2. [x] Anthropic adapter (`AnthropicBrain`, ADR 0002); live conformance pending an API key
  3. [x] OpenAI Responses adapter (`OpenAIBrain`); live conformance pending a key; Sign in with ChatGPT later
  4. [x] Kimi (Moonshot) and Grok (xAI) configured via `OpenAIBrain` + `base_url` (ADR 0002 #11); verify live
  5. [x] Router, single fallback, budget check, cost accounting (`enzo.brain.router`, `brains.toml`, ADR 0003)
  6. Live conformance (§1.4) on every configured brain: needs API keys
- [ ] Spec §7 tools: [x] typed registry (`enzo.tools`, `@tool`), validation before execution, errors as data,
  timeouts, parallel batches; [ ] real tools and connectors
- [x] Spec §4 agent loop (`enzo.agent`: `run_turn`, approvals, prompt order; ADR 0004)
- [x] Spec §11 persistence base: Postgres store for transcripts, brain/tool call ledgers, approvals (ADR 0005)
- [x] Gateway: conversations, SSE turns, approvals, brain choice, auth (`enzo.gateway`, `enzo.agent.service`, ADR 0006)
- [x] Web chat: streaming, approval cards with exact arguments, brain picker, history (`clients/web`)
- [x] Terminal chat (`clients/cli`, OpenTUI on Bun), sharing chat state with web via `@enzo/chat`
- [ ] Bring your own account (decision 2026-11-17):
  1. [x] Server: vault, connections index, live key check, `/connections` endpoints (ADR 0009,
     `docs/ops/provider-accounts.md`)
  2. [x] Clients: SDK + `/login` (hidden prompt), `/logout`, `/connections`, `/check`; web Accounts panel
  3. Model catalog and routing (ADR 0010): [x] server: tiers in `brains.toml`, task → tier, spread by
     conversation hash over connected providers, same-tier cross-provider fallback, rules classifier,
     tier pins, `done` names the answering brain; [ ] clients: `/models`, `/model`, show the brain per reply
  4. [ ] Sign in with ChatGPT (`enzo login chatgpt`, runs on the user's machine)
- [ ] Release automation (release PRs, CHANGELOG, tags)
- [ ] GitHub connector: issue #2
- Then continue in the order of the spec §17 checklist.
