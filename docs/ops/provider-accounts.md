# Provider accounts: setup and operations

Enzo uses the user's own model accounts. This is the setup and operations document spec §8.2 asks
for. Design: ADR 0009.

## Supported today

| Provider | How to connect |
| --- | --- |
| Anthropic (Claude) | API key (Anthropic forbids third-party apps from using Claude.ai logins) |
| OpenAI (GPT) | API key; Sign in with ChatGPT is planned |
| xAI (Grok) | API key |
| Moonshot (Kimi) | API key |

## Setup

- **Laptop:** nothing to do. Keys go to the OS keychain.
- **Server or container (no keychain):** create a vault key once and keep it secret:

```bash
cd server && uv run enzo-server --new-vault-key
```

Set it as `ENZO_VAULT_KEY` in the server's environment (a secret manager, not a committed file).

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
| Health check a saved key | `POST /connections/{provider}/check` → `{"ok": …, "detail": …}` |
| Disconnect (deletes the key) | `DELETE /connections/{provider}` |
| Rotate a key | connect again with the new key; the old one is replaced at once |

## Errors

| HTTP | Meaning | What to do |
| --- | --- | --- |
| 422 "rejected this key" | The provider refused the key | Create a new key, connect again |
| 502 "could not check the key" | Provider unreachable, rate limited or down | Retry later; nothing was saved |
| 409 "not connected" | No key saved for this provider | `/login <provider>` |
| 503 | No keychain and no `ENZO_VAULT_KEY` | Set `ENZO_VAULT_KEY` (see Setup) |
| 500 "cannot decrypt vault entry" | Wrong `ENZO_VAULT_KEY` or a modified `vault.json` | Restore the right key, or delete `vault.json` and reconnect |

## Where things live

- Keys: the OS keychain (service `enzo`), or `$ENZO_CONFIG_DIR/vault.json` encrypted (0600).
- `$ENZO_CONFIG_DIR/connections.json`: provider, method and time only. No secrets.
- Never in the database, transcripts, logs, or API responses.
