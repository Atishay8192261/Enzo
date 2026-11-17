# ADR 0003 — Brain routing, fallback and cost accounting

- **Status:** Accepted
- **Date:** 2026-11-17

## Context

Spec §1.3.3–§1.3.6 asks for per-task routing stored as configuration, one recorded fallback,
per-call accounting with honest cost, and a token budget enforced before the brain is called.

## Decisions

| # | Decision | Why |
|---|---|---|
| 1 | Brains and routing live in `enzo/brain/brains.toml` (packaged), validated by Pydantic; unknown fields, unknown brain names and self-fallbacks fail at load. | Routing must be explicit configuration, visible in review and logs (§1.3.3). Typos must fail loudly. |
| 2 | Keys are never in configuration: each brain names its environment variable. Adapters are built on first use. | §11.2. A missing key only matters for a brain actually routed to, and is reported with the variable to set. |
| 3 | Three task kinds: `light`, `hard`, `streaming`, plus a default and a per-call user override. | The spec's three routing cases (§1.3.3) and user choice (§1.3.6). |
| 4 | Fallback: one hop, only for rate limit, server, connection and timeout failures, never after text reached the user. A failed fallback raises `AllBrainsFailedError` listing every attempt. | §1.3.4 steps 1–4. Falling back mid-stream would show the user two half-answers. |
| 5 | Every attempt, failed or not, becomes a `CallRecord` (brain, provider, served model, tokens, latency, cost, outcome, `fallback_from`) sent to a ledger and the log. | §1.3.5, §13; a brain swap is never silent (§1.3.3). |
| 6 | Cost uses published prices with long-context tiers and per-retention cache-write prices; any missing price makes the cost `None`. Each price records its source and read date. | §1.3.5: "unknown, not estimated as zero". OpenAI and Anthropic price cache writes per retention. |
| 7 | The budget check counts tokens with the target brain and reserves `max_output_tokens`; over budget raises `PromptOverBudgetError` for the orchestrator to compact. | §1.3.5 and §5.2 (reserve tokens for generation). Tokenizers differ per brain. |
| 8 | Default routing: Claude Opus 5.5 (hard/default), Sonnet 5.5 (streaming), Haiku 5.5 (light), each falling back to the matching GPT tier and vice versa. Grok and Kimi are configured but not routed by default. | A default must exist; it is configuration, changed without code. Grok and Kimi are unproven until live conformance. |

## Evidence found while building

- The OpenAI adapter dropped billed cache-write tokens (`input_tokens_details.cache_write_tokens`). Fixed with a regression test that fails on the old code.
- Prices read on 2026-11-17: OpenAI from developers.openai.com/api/docs/pricing; Claude from the Claude API pricing table; xAI and Moonshot from their official pages via search. Moonshot's cache-write price conflicts between official pages, so it is left unknown.
