# ADR 0002 — Brain adapter contract

- **Status:** Accepted
- **Date:** 2026-11-17

## Context

Spec §1 requires a swappable brain: one interface, canonical tool schemas, honest capabilities,
one recorded fallback, and per-call accounting. Building the first adapter (Claude) surfaced
provider behaviour the canonical layer must absorb.

## Decisions

| # | Decision | Why (evidence) |
|---|---|---|
| 1 | Adapters disable SDK retries (`max_retries=0`). | The Anthropic SDK retries 429/5xx twice by default. Spec §1.3.4 / §8.3 forbid retry loops; the router owns the single fallback. A test asserts exactly one HTTP attempt per failure. |
| 2 | Assistant messages carry a `NativePart` holding the provider's raw output, replayed unchanged to the same provider and ignored by others. | Claude's thinking blocks must be sent back exactly in a tool loop. The payload uses `model_dump(mode="json", exclude_unset=True)`, verified byte-identical to what the SDK sends for `response.content`. |
| 3 | Canonical `Usage.input_tokens` is the whole prompt; cache read/write are subsets. | Providers disagree (Anthropic reports uncached input only). One definition keeps cost accounting comparable across brains. |
| 4 | `Capabilities.forced_tool_choice` is explicit; `tool_call()` uses `auto` when forcing is unavailable. | Forced `tool_choice` returns 400 on current Claude models (Opus 5.5, Sonnet 5.5). Enzo must not claim a capability a brain lacks (§1.3.2). |
| 5 | Adapters stream internally even for `chat()`. | Avoids HTTP timeouts on long generations (SDK guidance). |
| 6 | `BrainResponse.model` is the model that actually served the reply. | A provider-side swap must never be silent (§1.3.3). |
| 7 | Connection failures get their own error kind and are fallback-eligible. | An unreachable provider is a server-side failure in the sense of §1.3.4, but logs must say what really happened. |
| 8 | Provider-side refusal fallbacks are not enabled in the adapter. | They swap the serving model inside one call. Any brain swap must go through the router so it is configured and logged (§1.3.3). Revisit in the router slice. |
| 9 | OpenAI runs on the Responses API with `store=False`, reading raw stream events. | Enzo owns history (privacy, replay). The SDK's `responses.stream()` helper adds `parsed` fields the API never sent, breaking exact replay, and its `get_final_response()` raises on a normal max-token stop. Both reproduced in tests. |
| 10 | OpenAI function tools are sent with `strict: false`. | Pydantic schemas with optional fields are not strict-mode compatible; Responses would silently fall back anyway. Enzo validates every call itself (spec §4.2). |
| 11 | One Responses adapter serves OpenAI-compatible providers (xAI, Moonshot) via `base_url` and a `provider` name. | Both document an OpenAI-compatible Responses endpoint. To be proven live per provider before it is listed as supported (§1.4, §16). |

## Consequences

- Persisted transcripts store provider payloads; they stay JSON-serialisable.
- Live conformance (§1.4) still needs API keys: offline tests run the real SDK against simulated HTTP.
