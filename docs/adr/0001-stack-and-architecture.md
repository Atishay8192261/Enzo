# ADR 0001 — Stack and architecture

- **Status:** Accepted
- **Date:** 2026-11-17
- **Decider:** Atishay Jain

## Context

The spec (`docs/spec/ENZO_SPEC.md`) deliberately prescribes no stack. It requires every
component in §2.3 to have a clear boundary and its own test, and the LLM brain to be
swappable behind one interface (§1.3.1).

## Decision

| Concern | Choice | Why |
|---|---|---|
| Agent core + gateway | Python 3.13, FastAPI, Pydantic v2, uv | Pydantic gives typed tool schemas → JSON Schema for free (§7.2.1). FastAPI ships native SSE for streaming (§3.2 step 4). |
| Clients | React + Vite (web), Ink (terminal), one shared TS SDK | Two surfaces, one gateway, one memory (§10). |
| Persistence | Postgres + pgvector (planned) | Structured records, vectors and the job queue in one durable store (§11). |
| Agent framework | **None — hand-built** | The provider interface, loop, memory tiers and safety layer are the product (§1, §4, §6, §12). |
| Quality gates | ruff, mypy `--strict`, pytest (warnings = errors); Biome, `tsc` 7, Vitest | Fail loudly. |
| CI | GitHub Actions, actions pinned to commit SHAs, `contents: read` | Supply-chain hygiene. |

## Brain providers and auth (verified 2026-11-17)

Label key follows spec §0.

| Brain | Auth Enzo will use | Evidence |
|---|---|---|
| Claude | **API key only.** | *Verified:* Anthropic's legal & compliance page forbids third-party apps from offering Claude.ai login or routing through Free/Pro/Max/Ultra plans. |
| ChatGPT / GPT | "Sign in with ChatGPT" open-source flow (user's plan pays), API key fallback. | *Verified:* OpenAI SIWC docs — open-source clients may use the user's plan via the public Responses API with OAuth. Requires Enzo to stay open source. |
| Grok | API key. Subscription OAuth only as an **experimental** adapter. | *Unverified:* used by third-party agents, but no published xAI terms; some users get HTTP 403. Per §1.3.2 Enzo must not claim it works until tested. |
| Kimi K3 / K3.1 | Moonshot API (OpenAI-compatible) or a router. Not self-hosted. | *Verified:* open weights released July 2026; ~1.4 TB at MXFP4, multi-node GPUs — not viable for a personal deployment. |

## Consequences

- More code to own than a framework, in exchange for components that are each testable
  without a live LLM, browser or user (§2.3).
- The OpenAI subscription path ties Enzo to remaining open source.
- Every adapter must pass the same conformance suite before it is listed as supported (§1.4).
