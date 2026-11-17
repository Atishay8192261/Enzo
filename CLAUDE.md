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

## Git and GitHub

- Branch from `main`: `feat/…`, `fix/…`, `chore/…`, `docs/…`.
- Commits: one line, Conventional Commits, e.g. `feat(brain): add provider fallback with single retry`.
- Every change lands through a PR that fills `.github/pull_request_template.md`. CI must be green.
- Merge method: rebase-merge. Delete the branch after merge.
- Cloud sessions: GitHub GraphQL is blocked, so use `gh api` REST endpoints. They cannot create repos,
  delete branches, or approve PRs (GitHub never allows approving your own PR). The repo may be checked
  out as `Enzo` or `enzo`.

## Stack (ADR 0001)

- `server/`: Python 3.13, FastAPI, Pydantic v2, uv. ruff, mypy `--strict`, pytest with warnings as errors.
- `clients/sdk`: the only TS code that talks to the gateway. `clients/web`: React + Vite. `clients/cli`: Ink.
- Biome (lint/format, Markdown excluded), TypeScript 7, Vitest. pnpm workspace.
- Postgres + pgvector planned for persistence. No agent frameworks: brain adapters, loop, memory and
  safety are hand-built on official provider SDKs.

## Commands

```bash
cd server && uv sync && uv run ruff format --check . && uv run ruff check . && uv run mypy src tests && uv run pytest
```

```bash
pnpm install && pnpm check && pnpm typecheck && pnpm test && pnpm --filter @enzo/web build
```

Run locally: `cd server && uv run enzo-server` (port 8787), then `pnpm --filter @enzo/cli start` or
`pnpm --filter @enzo/web dev`.

## Gotchas already found

- Browsers throw "Illegal invocation" when `fetch` is called as a method of another object; the SDK wraps it.
- Starlette's `TestClient` needs `httpx2`, not `httpx`.
- Biome formats Markdown by default; it is excluded so the spec stays verbatim.
- FastAPI has native SSE (`fastapi.sse`); no extra streaming library needed.
- Pydantic `model_validate_json(..., strict=True)` with `extra="forbid"` rejects coerced types and unknown
  fields: use it to validate tool-call arguments from the brain.
- Never name a module after a stdlib module (e.g. `types.py`): scripts run from that folder break.
- Tests: shared helpers live in normal modules (`tests/brain/schemas.py`), never imported from `conftest.py`.
- Prove a test has teeth: break the code on purpose, watch it fail, restore.
- Provider SDKs retry on their own (Anthropic: 2 retries). Adapters must set `max_retries=0` (ADR 0002).
- Current Claude models (Opus 5.5, Sonnet 5.5) reject forced `tool_choice` with a 400.
- Claude thinking blocks must be replayed unchanged: `model_dump(mode="json", exclude_unset=True)`.
- Cloud sessions: `api.anthropic.com` bypasses the egress proxy, so a key saved as a network secret is
  not injected there. Live Claude tests need another route.
- OpenAI: never use `responses.stream()` / `get_final_response()` in adapters: parsed wrappers add fields
  that break replay, and an "incomplete" (max tokens) stop raises. Read raw events (ADR 0002 #9).
- `curl` reaches `developers.openai.com`, `WebFetch` does not (different network path): fetch docs with
  `curl` (`/llms.txt` lists Markdown pages).

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
  4. Kimi (Moonshot) and Grok (xAI) via `OpenAIBrain` + `base_url` (ADR 0002 #11); verify base URLs live
  5. Router, single fallback, token and cost accounting (cost "unknown" when unpublished, never 0)
- [ ] Release automation (release PRs, CHANGELOG, tags)
- [ ] GitHub connector: issue #2
- Then continue in the order of the spec §17 checklist.
