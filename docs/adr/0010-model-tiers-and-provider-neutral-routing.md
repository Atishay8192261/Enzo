# ADR 0010 — Model tiers and provider-neutral routing

- **Status:** Accepted (2026-11-18). Implements the decision of 2026-11-17 (bring your own account).
- **Supersedes:** ADR 0003 decisions 3 (brain per task) and 8 (default routing and fixed fallback map)
- **Spec:** §1.3.3 routing, §1.3.4 fallback, §1.3.6 user choice

## Context

Enzo ships no models; each user connects their own provider accounts. Routing to fixed brain names
(Opus for hard, Sonnet for streaming, …) breaks as soon as a user has no Anthropic key, and it
favours one provider. Atishay decided: a catalog grouped fast / balanced / powerful with
recommendations, balanced by default (Claude Sonnet 5.5, GPT-6.2 Sol), and smart per-task routing
spread across the providers the user connected.

## Decision

1. **Tiers in configuration.** Every brain in `brains.toml` has `tier` (fast | balanced | powerful),
   optional `recommended` and a sourced `note`. Tasks map to tiers: light → fast, streaming →
   balanced, hard → powerful; anything else → `routing.default` (balanced).
2. **Only connected providers.** A brain is a candidate only if its provider has a key (connected
   account or environment variable). Nothing connected → `NoBrainAvailableError` that lists the
   `/login` commands; nothing is sent anywhere.
3. **Spread, then stick.** Connected providers are ordered by a stable hash (SHA-256) of the
   conversation id. Different conversations spread evenly across providers; one conversation stays
   with one provider for every tier, so prompt caches stay warm (ADR 0007) and replayed reasoning
   stays with its provider.
4. **Pick per provider.** Its recommended brain of the tier, else any brain of the tier, else the
   nearest tier: stronger first. A weaker brain is used only when no provider has the tier or a
   stronger one (§1.3.4 "never silently degrade").
5. **Fallback.** The next connected provider's candidate, same ranking: one hop, recorded, only for
   transient failures (unchanged from ADR 0003 #4).
6. **Classify once per turn, by rules.** A new message longer than `long_message_chars` (20,000)
   is a hard task (long-document reasoning); otherwise a live reply (balanced). Only the user's own
   words count, not context or memory. No model call, so no added latency or cost. The task is fixed
   for the whole turn: the brain never changes mid-task (§1.3.3).
7. **User choice.** A conversation can pin a brain (`claude-opus`) or a tier (`powerful`). Brain
   names may not equal tier names. A pinned brain still falls back to another provider's brain of
   the same tier.
8. **Visible.** Every route is logged with task, tier, provider order and any tier substitution.
   The `done` event names the brain and served model that answered (§1.3.6). `GET /brains` lists
   tier, recommendation, note and whether each brain is available now.
9. **Grok and Kimi join routing** once their provider is connected (the user chose to connect
   them). Placement, verified 2026-11-17: Grok 4.7 is xAI's frontier model and its only public-API
   model (powerful); Kimi K3.1 Pro is Moonshot's flagship (powerful, recommended); Kimi K3 is
   superseded by it (listed, not recommended).

## Consequences

- Any single connected provider is enough; two or more spread the work and back each other up.
- A provider without a balanced model (xAI, Moonshot today) answers balanced tasks with its
  powerful model when the conversation is assigned to it: better answers, higher cost.
- Rule-based classification is coarse. An LLM triage step (fast tier) could be added later if
  rules misroute; it would cost a call and latency per turn.
- Not yet verified live: replaying one model's reasoning to another model of the same provider
  between turns. Live conformance (§1.4) must cover a model switch inside one conversation.
