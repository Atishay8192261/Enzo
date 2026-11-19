# Provider accounts: setup and operations

Enzo uses the user's own model accounts. This is the setup and operations document spec §8.2 asks
for. Design: ADR 0009.

## Supported today

| Provider | How to connect |
| --- | --- |
| Anthropic (Claude) | API key (Anthropic forbids third-party apps from using Claude.ai logins) |
| OpenAI (GPT) | **Sign in with ChatGPT** (your ChatGPT plan pays, no key), or an API key (ADR 0013) |
| xAI (Grok) | API key |
| Moonshot (Kimi) | API key |

## Service accounts (used by tools)

| Service | What connecting it does | Without it |
| --- | --- | --- |
| Tavily | Raises web search limits (1,000 free searches a month, no card) | Search still works, keyless and rate-limited |
| GitHub | Issues, pull requests, CI and releases on your repos (fine-grained token, setup in `docs/connectors/github.md`) | No GitHub tools |

Connect with `/login tavily` or the web Accounts panel, like a model provider. The key is checked
with Tavily's usage endpoint (no credits spent), then kept in the vault.

## Setup

- **Laptop:** nothing to do. Keys go to the OS keychain.
- **Server or container (no keychain):** create a vault key once and keep it secret:

```bash
cd server && uv run enzo-server --new-vault-key
```

Set it as `ENZO_VAULT_KEY` in the server's environment (a secret manager, not a committed file).

## Sign in with ChatGPT

Use your ChatGPT plan instead of an API key. Nothing to set up first.

1. **Terminal:** `/login chatgpt`. **Web:** Accounts → **Continue with ChatGPT**.
2. A browser tab opens on OpenAI's sign-in page. If no tab opens, the app shows a local link
   (`http://127.0.0.1:8787/auth/chatgpt/…`) to open by hand.
3. Sign in, name the app (Enzo suggests "Enzo") and allow it to use your plan. The first time,
   OpenAI also asks which workspace it is for.
4. The tab says "Signed in to Enzo" and the app says which account is connected.

What to know:

- **Usage:** GPT replies count toward your plan. Review or limit Enzo's share at
  <https://chatgpt.com/settings/usage>. On Plus, the five-hour limit is shared with ChatGPT and
  other apps. Replies show "Using ChatGPT plan" instead of a price.
- **Models:** only models your plan includes can be picked. Others show as unavailable.
- **Staying signed in:** Enzo renews the hour-long access token by itself. If the sign-in ends (you
  disconnected Enzo in ChatGPT settings, or 30 days without use), Enzo says
  "Sign in again: /login chatgpt".
- **Another account:** `/login chatgpt new` registers a different ChatGPT account and replaces the
  current one. Signing in again normally reuses the account Enzo knows.
- **Sign out:** `/logout openai` (web: Sign out). Enzo ends the session with OpenAI and forgets the
  tokens. If OpenAI could not confirm it, disconnect Enzo in your ChatGPT settings.
- **Where it works:** on the computer running Enzo. The browser comes back to the gateway on
  `127.0.0.1`, and `auth.openai.com` and `api.openai.com` must be reachable.
- **Switching to a key:** connecting an OpenAI API key replaces the ChatGPT sign-in (and ends its
  session), and the other way round.

## Connect from the apps

- **Terminal:** `/login <provider>` opens a hidden prompt: paste the key (only dots are drawn),
  Enter saves, Esc cancels. `/connections` lists providers, `/check <provider>` tests a saved key,
  `/logout <provider>` deletes it. A key typed on the command line itself is refused.
- **Web:** open **Accounts**: one password field per provider, plus Check and Disconnect.

## Operations

| Task | API |
| --- | --- |
| List providers, models and connection state | `GET /connections` |
| Connect (key is checked with the provider first) | `PUT /connections/{provider}` `{"api_key": "…"}` |
| Start Sign in with ChatGPT | `POST /connections/openai/chatgpt` `{"new_account": false}` → `{"login_id", "url"}` |
| Follow a sign-in | `GET /connections/openai/chatgpt/{login_id}` → `{"status", "message", "first_time", "usage_url"}` |
| Health check a saved key or sign-in | `POST /connections/{provider}/check` → `{"ok": …, "detail": …}` |
| Disconnect (deletes the key, or signs out) | `DELETE /connections/{provider}` → `{"remote_sign_out": "confirmed" \| "not_confirmed" \| null}` |
| Rotate a key | connect again with the new key; the old one is replaced at once |

## Errors

| HTTP | Meaning | What to do |
| --- | --- | --- |
| 422 "rejected this key" | The provider refused the key | Create a new key, connect again |
| 502 "could not check the key" | Provider unreachable, rate limited or down | Retry later; nothing was saved |
| 409 "not connected" | No key saved for this provider | `/login <provider>` |
| 503 | No keychain and no `ENZO_VAULT_KEY` | Set `ENZO_VAULT_KEY` (see Setup) |

ChatGPT sign-in messages:

| Message | Meaning | What to do |
| --- | --- | --- |
| "…was not allowed to use your ChatGPT plan" | Plan use was declined on the consent screen | `/login chatgpt` again and allow it, or use an API key |
| "That is a different ChatGPT account…" | The browser signed in as someone else | Sign in as the same account, or `/login chatgpt new` |
| "Your ChatGPT plan's usage limit for Enzo is reached" | Plan or app limit hit | Review it at chatgpt.com/settings/usage |
| "This ChatGPT account or workspace can't use its plan in Enzo" | Not eligible (plan, workspace policy) | Use an API key: `/login openai` |
| "Your ChatGPT sign-in has ended" | The renewal was refused | `/login chatgpt` |
| 500 "cannot decrypt vault entry" | Wrong `ENZO_VAULT_KEY` or a modified `vault.json` | Restore the right key, or delete `vault.json` and reconnect |

## Keys from environment variables (developers only)

Enzo ignores provider keys exported in the shell (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, …) unless
the gateway runs with `ENZO_USE_ENV_KEYS=1`. Otherwise a key in your shell would be used, and
billed, while the app says "not connected" (issue #24). When opted in, those providers show as
connected with method `environment`; a connected account still takes precedence, and the app
cannot disconnect an environment key (unset the variable instead).

## Where things live

- Keys: the OS keychain (service `enzo`), or `$ENZO_CONFIG_DIR/vault.json` encrypted (0600).
- `$ENZO_CONFIG_DIR/connections.json`: provider, method and time only. No secrets.
- Never in the database, transcripts, logs, or API responses.
