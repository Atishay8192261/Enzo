# Enzo

A stateful, multi-client, tool-using personal agent with a **swappable LLM brain**.

Enzo is not a prompt wrapper. It is an operating system around an LLM: a gateway, an
orchestrator running a plan → act → observe loop, a typed tool registry, four-tier memory,
a background runtime and a safety layer with approval gating. The LLM is a replaceable part.

> **Status: in progress.** Nothing below is claimed as done until its box in the
> [build checklist](docs/spec/ENZO_SPEC.md#17-build-verification-checklist--to-be-completed-during-build)
> is checked with evidence.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/Atishay8192261/Enzo/main/install.sh | sh
```

Or `uv tool install enzo-agent`. Then run `enzo` (terminal, a new chat; `enzo -c` continues the last), `enzo web` (browser), `enzo doctor` (check
the installation), `enzo logs` (what Enzo did, for a bug report), `enzo stop` (turn off Enzo left running for reminders) or `enzo update`. Python, the database and the terminal app come with it: nothing
else to install. macOS and Linux; on Windows, use WSL. Releases: [docs/ops/release.md](docs/ops/release.md).

## Docs

- [Specification](docs/spec/ENZO_SPEC.md) — the source of truth
- [ADR 0001 — stack and architecture](docs/adr/0001-stack-and-architecture.md)
- [Provider accounts: setup and operations](docs/ops/provider-accounts.md)
- [Testing Enzo on your own computer](docs/handoff/laptop-test-agent.md)

## Layout

```
server/        Python agent core + gateway (FastAPI)
clients/sdk/   Typed TS client for the gateway, shared by every surface
clients/web/   Web UI (React + Vite)
clients/cli/   Terminal UI (OpenTUI, runs on Bun)
clients/chat/  Chat state shared by web and terminal
docs/          Spec and architecture decision records
```

## Develop

One command from a clone. It checks your tools, installs dependencies, creates the database,
runs migrations, starts the gateway and opens the terminal app; quitting the app stops the gateway.

```bash
scripts/dev.sh
```

`scripts/dev.sh web` opens the web app instead, `scripts/dev.sh doctor` only checks, and
`pnpm dev` runs the same script. When a tool is missing or the wrong version, it says what to
install and stops.

**Tools it needs:** Node 22.12+ (24 LTS recommended), pnpm via `corepack enable`, Bun 1.4.4
(pinned in `.bun-version`), [uv](https://docs.astral.sh/uv/) (it installs Python 3.13) and
PostgreSQL 16.

- **macOS (Homebrew):** `brew install postgresql@16 && brew services start postgresql@16`. The
  script creates the `enzo` role and database itself, because Homebrew makes your login user a
  database superuser. On Apple Silicon, install Node with the official arm64 installer from
  nodejs.org if an Intel Homebrew cannot build it.
- **Linux (Debian/Ubuntu):** `sudo apt-get install -y postgresql-16`. The script never uses sudo,
  so it prints the two `sudo -u postgres` commands that create the role and database.

## Checks

```bash
cd server && uv run ruff format --check . && uv run ruff check . && uv run mypy src tests && uv run pytest
```

```bash
pnpm check && pnpm typecheck && pnpm test
```
