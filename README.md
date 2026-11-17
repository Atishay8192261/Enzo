# Enzo

A stateful, multi-client, tool-using personal agent with a **swappable LLM brain**.

Enzo is not a prompt wrapper. It is an operating system around an LLM: a gateway, an
orchestrator running a plan → act → observe loop, a typed tool registry, four-tier memory,
a background runtime and a safety layer with approval gating. The LLM is a replaceable part.

> **Status: in progress.** Nothing below is claimed as done until its box in the
> [build checklist](docs/spec/ENZO_SPEC.md#17-build-verification-checklist--to-be-completed-during-build)
> is checked with evidence.

## Docs

- [Specification](docs/spec/ENZO_SPEC.md) — the source of truth
- [ADR 0001 — stack and architecture](docs/adr/0001-stack-and-architecture.md)

## Layout

```
server/        Python agent core + gateway (FastAPI)
clients/sdk/   Typed TS client for the gateway, shared by every surface
clients/web/   Web UI (React + Vite)
clients/cli/   Terminal UI (OpenTUI, runs on Bun)
clients/chat/  Chat state shared by web and terminal
docs/          Spec and architecture decision records
```

## Quickstart

Requires Python 3.13+ with [uv](https://docs.astral.sh/uv/), Node 22.12+, pnpm 10, [Bun](https://bun.sh) 1.3.14+
(the terminal app) and Postgres 16 with pgvector (see `CLAUDE.md`).

```bash
pnpm install
```

```bash
cd server && uv sync && uv run enzo-server
```

In a second terminal, either surface:

```bash
pnpm --filter @enzo/cli start
```

```bash
pnpm --filter @enzo/web dev
```

## Checks

```bash
cd server && uv run ruff format --check . && uv run ruff check . && uv run mypy src tests && uv run pytest
```

```bash
pnpm check && pnpm typecheck && pnpm test
```
