# ADR 0009 — Provider credentials live in a vault, checked before they are saved

- **Status:** Accepted (2026-11-17)
- **Context links:** spec §8.2 (auth flow, status check), §8.3 (auth failures), §11.2 (what must
  never be stored), §11.3 (search logs for a test secret); decision "bring your own account"

## Context

Enzo ships no models: each user connects their own provider accounts. Spec §11.2 says API keys and
tokens "live only in a secure vault or the provider's OAuth store, never in memory, files, logs,
or transcripts". Enzo runs on laptops (which have an OS keychain) and on servers or containers
(which usually do not).

## Decision

1. **Vault order:** `ENZO_VAULT_KEY` set → an encrypted file (`vault.json`); else the OS keychain
   through `keyring` (macOS Keychain, Windows Credential Locker, Secret Service); else **refuse**
   with a message that says how to create a key (`enzo-server --new-vault-key`).
2. **Never plaintext:** keychain backends that are fake (`fail`, `null`), plaintext
   (`keyrings.alt`) or low priority (< 1) are rejected. There is no plaintext fallback.
3. **Encrypted file format:** AES-256-GCM per entry (`cryptography`), a random 96-bit nonce per
   write, and the entry name as associated data, so an entry copied under another name fails to
   decrypt. A wrong key or a modified file raises `VaultCorruptError`; nothing is guessed.
4. **Files:** written atomically (temp file + `os.replace`), owner-only (0600) from creation,
   in a 0700 directory (`ENZO_CONFIG_DIR`, default `~/.config/enzo`).
5. **Index without secrets:** `connections.json` holds only provider, method and time, validated
   strictly when read, so the UI can list connections without touching the vault.
6. **Check before save:** a key is tried against the provider (list models: read-only, free,
   one attempt, no SDK retries) and saved only if accepted. `POST /connections/{p}/check`
   re-checks a saved key (spec §8.2 status check).
7. **Key wins over environment:** a connected account is used before `*_API_KEY` variables. A
   key change drops that provider's cached adapters at once (`Router.forget`).
8. **Nothing echoes a key:** connection responses never include it; the gateway's 422 handler
   drops the submitted value from every validation error (FastAPI echoes it by default).

## Consequences

- Laptops need no setup: the OS keychain is used.
- Servers and containers must set `ENZO_VAULT_KEY`. Whoever holds it can read the vault, so it is
  handled like any deployment secret. Losing it means reconnecting accounts, not data loss.
- A provider outage blocks connecting (the key cannot be checked) rather than saving an unchecked
  key. The error says so plainly ("could not check the key", HTTP 502), distinct from "rejected".
- OAuth logins (Sign in with ChatGPT) add a new credential type to the same vault later.
