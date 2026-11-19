# ADR 0013 — Sign in with ChatGPT: the user's plan pays for GPT replies

- **Status:** Accepted (2026-11-18). Decided with Atishay on 2026-11-17 (bring your own account); built
  first so he can test Enzo with a ChatGPT login, no API key.
- **Spec:** §1.3 (swappable brain, cost per call), §8.2 (setup doc, auth, status check),
  §11.2 (secrets only in the vault), §12.5 (privacy)
- **Source:** OpenAI's "ChatGPT plan usage" docs for open-source, locally hosted apps
  (developers.openai.com/siwc, read 2026-11-18). Ops guide: `docs/ops/provider-accounts.md`.

## Context

Most people who want a personal agent already pay for ChatGPT. Asking them to create an API key,
add a card and pay per token is the biggest hurdle to trying Enzo. OpenAI now publishes an official
flow for open-source, local apps: the user signs in with ChatGPT, allows the app to use their plan,
and the app calls the **public** Responses API with the OAuth access token. No client secret, no
partner key, no private endpoint.

## Decision

1. **The documented flow, step by step** (`enzo.accounts.chatgpt`):
   - **Host id:** a stable, opaque `urn:uuid:…` per installation, in `~/.config/enzo/host-id`,
     created before the first sign-in and sent with every one.
   - **Authorization:** code + PKCE (S256) + OpenID Connect, with fresh `state`, `nonce` and verifier
     per attempt. The first sign-in uses `client_id=dynamic_agent_client` with
     `agent_name_hint=Enzo`. OpenAI registers a client for that user and returns its id, which every
     later sign-in reuses.
   - **Code exchange and checks:** the ID token is verified against OpenAI's JWKS. Asymmetric
     algorithms only, plus issuer, audience (the issued client), expiry and nonce. The granted
     scopes must include `chatgpt.tokens.use.direct`, or nothing connects.
   - **Returning sign-in:** it must come back as the same account (`sub`) and client, or it changes
     nothing.
2. **The callback is the gateway itself:** `http://127.0.0.1:<gateway port>/auth/callback`. OpenAI
   lets the loopback port vary, so no second listener is needed.
   - The app gets a short local link, `/auth/chatgpt/{login_id}`, which redirects to OpenAI. So the
     full authorization address never passes through an app, its output or its logs.
   - The two browser routes carry no bearer token (a browser can't send one). A random login id and
     single-use state protect them, and access logs drop their query string.
3. **Tokens live in the vault** as a `chatgpt` credential next to API keys (ADR 0009). The index
   keeps only non-secret facts: the method, the account's email as a label, and the models the
   plan lists.
   - Access tokens last an hour, so they are renewed two minutes before expiry, one renewal at a time
     because refresh tokens rotate.
   - When a renewal is refused for good (`invalid_grant` and similar), the tokens are forgotten and
     the user is told to sign in again.
   - When OpenAI is only unreachable or down, nothing is erased.
   - The registration (client id, account) survives sign-out, so signing in again reuses it.
4. **One OpenAI account, two ways in.** `openai` lists the methods `chatgpt` and `api_key`.
   Connecting one replaces the other, and the replaced ChatGPT session is revoked with OpenAI.
   Sign-out revokes the refresh token at OpenAI's `revocation_endpoint`. If OpenAI doesn't confirm,
   the tokens are forgotten anyway and the user is told how to end the session in ChatGPT settings.
5. **Plan mode in `OpenAIBrain`** follows OpenAI's preview limits for this route:
   - The access token comes from the SDK's own async `api_key` provider, called before every
     request, so a renewed token is used at once.
   - Requests set `store: false` and `stream: true`, and never send `max_output_tokens`.
   - Function tools go inside one `enzo` namespace, and replayed calls name that namespace.
   - There is no token-counting endpoint, so the capability says `unsupported`.
   - OpenAI's plan error codes get plain words, including the usage limit with its
     `chatgpt.com/settings/usage` link. A limit reached mid-stream is a rate limit, so routing may
     fall back to another account the user connected.
6. **Cost:** a reply the plan paid for has no per-token price.
   - Its ledger row says `billing = "plan"` (migration 0002), and `cost_usd` stays NULL.
   - The `done` event carries `plan: true`, so apps show "Using ChatGPT plan" with a
     Manage usage link, as OpenAI's UI guidelines ask.
   - The first sign-in on a computer says once that replies now use the plan.
7. **Model availability follows the plan.** After sign-in, Enzo reads the account's model list.
   Catalog models that are not on it are shown but can't be picked (`/brains` `available: false`).

## Consequences

- People can use Enzo with only a ChatGPT login. Their plan's limits apply, and for Plus the
  five-hour limit is shared with ChatGPT and other apps.
- `auth.openai.com`, `api.openai.com` and the sign-in page must be reachable from the computer
  running Enzo. Cloud sessions block them, so the live flow is verified on the user's laptop.
- One ChatGPT account at a time. `/login chatgpt new` registers another and replaces the active one.
  A multi-account picker can come later.
- Enzo must stay open source and locally hosted to use this flow. A hosted or paid Enzo would need
  OpenAI's partner path instead.
