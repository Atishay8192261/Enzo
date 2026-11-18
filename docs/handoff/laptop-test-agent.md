# Handoff: the laptop test agent

You are Claude Code running on Atishay's Mac, in his clone of Enzo (`~/projects/enzo`). Read
`CLAUDE.md` first, then this file. Re-read this file after every `git pull`: it changes as Enzo grows.

## Your job, in one line

Be Atishay's hands-on test partner: install and run Enzo on this Mac, use it the way a real user
would, help him do the same, and turn everything that is broken, confusing or missing into a clear
GitHub issue.

- You **test, run, observe and report**. You do not build features.
- A separate Claude Code session in the cloud keeps building Enzo and merging to `main`. It reads
  your issues and fixes them. Two builders editing the same code would collide, so you don't.
- Small fixes only when Atishay asks for one: branch `fix/…`, a PR that fills the template, never a
  push to `main`.
- Talk to Atishay the way `CLAUDE.md` says: short bullets, plain words, lead with the answer, never
  hide a problem.

## Rules that never bend

- **Secrets:** Atishay must never paste an API key into the Claude chat or into a terminal command.
  Keys go only into Enzo's own hidden prompt (`/login <provider>` in the terminal app) or the
  password field in the web app's **Accounts** panel. If a key appears in chat, logs, a command or a
  file, stop, tell him, and ask him to revoke that key with the provider.
- Never print or `cat` the vault, never commit `.env` files, never put a key in an issue. Redact
  logs before quoting them (`sk-…` → `[redacted]`).
- Never edit `docs/spec/ENZO_SPEC.md`.
- Ask before anything destructive: dropping the database, deleting `~/.config/enzo`, killing
  processes you did not start.
- Spending: every real message costs money on Atishay's own accounts. Prefer the fast and balanced
  tiers for routine tests, and tell him before a test that sends long prompts or many turns.

## What Enzo is today (2026-11-18)

Enzo is a personal AI agent with a swappable model "brain". There are three running pieces:

| Piece | What it is | Start it with |
| --- | --- | --- |
| Gateway | Python server: conversations, routing, accounts | `cd server && uv run enzo-server` (port 8787) |
| Terminal app | OpenTUI chat on Bun | `pnpm --filter @enzo/cli start` |
| Web app | React chat in the browser | `pnpm --filter @enzo/web dev`, then open the URL Vite prints |

**Works now:**

- **Accounts.** You connect your own provider accounts: Anthropic (Claude), OpenAI (GPT), xAI (Grok)
  and Moonshot (Kimi).
  - Each key is checked with the provider before it is saved.
  - Keys are kept in the macOS Keychain under service `enzo`.
  - Terminal: `/login <provider>`, `/logout <provider>`, `/connections`, `/check <provider>`.
  - Web: the **Accounts** panel.
- **Chat.** Streaming replies. The conversation survives a restart.
  - Terminal: the conversation id is kept in `~/.config/enzo/conversation`.
  - Web: it is kept in the browser's local storage.
- **Routing (ADR 0010).** Models are grouped fast / balanced / powerful.
  - Normal messages go to balanced: Claude Sonnet 5.5 or GPT-6.2 Sol.
  - A message over 20,000 characters goes to powerful.
  - Each conversation is assigned to one connected provider. Different conversations spread
    across providers.
  - On a rate limit or outage, Enzo tries one other provider at the same tier.
- **Choosing a model.** Terminal: `/models` lists the catalog by tier (★ = recommended, plus what
  each model needs). `/model <name|fast|balanced|powerful|auto>` picks a model, any model of a tier,
  or smart routing. Web: the Model menu, grouped by tier. Models without a connected account are
  shown but cannot be picked.
- **Which model answered.** Every live reply shows the model that wrote it (terminal: `· name` after
  the reply; web: small text under it). Replies loaded from history don't show it yet.
- **Approvals.** A risky action waits for an approval showing the exact arguments. No real tool
  exists yet, so you cannot trigger one in normal use.
- **Cost.** Shown after each reply, or "cost unknown" when a provider publishes no price.

**Not built yet** (don't file these as bugs; do note how much their absence hurts real use):

- **No real tools:** no web search, files, email, calendar or anything else. Enzo can only talk.
- **No memory** across conversations (spec §6). No context compaction for very long chats (§5).
- **Sign in with ChatGPT** (use the ChatGPT plan instead of an API key) is not built. It will need
  this Mac, because the login runs in the local browser. You will help test it.
- **Not yet verified against the real APIs:** Grok, Kimi, and switching models inside one
  conversation.
- No background jobs, no scheduled tasks, no deployment.

## One-time setup on this Mac

Check every version against what is installed. Don't trust this list blindly; report any mismatch.

1. **Tools.** Use Homebrew, unless a tool is already installed.
   - `git` and `gh` (logged in: `gh auth status`).
   - `uv`: it installs Python 3.13 by itself.
   - Node 22.12 or newer (CI uses 24).
   - pnpm 10: run `corepack enable`. The repo pins `pnpm@10.28.0`.
   - Bun **1.4.4**, the exact version CI uses. Check with `bun --version`.
   - PostgreSQL 16. pgvector is not used yet, so skip it if it's awkward to install.
2. **Database.** Start Postgres. With Homebrew: `brew services start postgresql@16`.
   - Create the role and database that `CLAUDE.md` names: role `enzo`, password `enzo`, superuser
     (the tests create throwaway databases), database `enzo`.
   - Homebrew's admin user is the Mac login user, so drop the `sudo -u postgres` part of the
     commands in `CLAUDE.md`.
3. **Code.**
   - `git pull` in `~/projects/enzo`.
   - `pnpm install` at the root.
   - `cd server && uv sync && uv run alembic upgrade head`.
4. **Prove it works before using it.**
   - Run both check commands in `CLAUDE.md`. Every test should pass.
   - If anything fails here, that is your first issue.
5. **Keychain.** Enzo saves keys through the `keyring` library into the login Keychain.
   - macOS may ask whether Python can use the Keychain. That prompt is expected: allow it for this
     Python only.
   - Note whether it asked, and how often: repeated prompts are a usability bug.
   - If Enzo says "no secure place for credentials", the Keychain was not usable. Report it.

## Running it

- Use three terminals (or background processes you started and will stop):
  1. Gateway: `cd server && uv run enzo-server`. Its logs are your first evidence for any bug.
  2. Terminal app: `pnpm --filter @enzo/cli start`.
  3. Web app: `pnpm --filter @enzo/web dev`.
- **Updating:** `git pull`, then `pnpm install`, `cd server && uv sync && uv run alembic upgrade head`.
  Then restart the gateway.
- **Gateway URL:** the terminal app uses `ENZO_GATEWAY_URL`, default `http://127.0.0.1:8787`. The
  web app uses the same default for its `/api` proxy.

## What to test: real user journeys

For each journey:
1. Have Atishay do it himself where he can: it's his experience that matters.
2. Watch the gateway log while he does.
3. Write down what was expected against what happened.

1. **First run, nothing connected.** Start everything and send "hi".
   - Expect a plain message telling you to connect an account with `/login …`.
   - It must not be a crash or a raw stack trace.
2. **Connect an account.**
   - In the terminal, `/login anthropic`, then paste the key at the hidden prompt. Only dots may
     appear. Then try the web Accounts panel.
   - Expect "Connected anthropic".
   - Then try a wrong key: expect "anthropic rejected this key", with nothing saved.
3. **Chat.**
   - Ask a few ordinary questions and judge the replies: speed, streaming, how long until the first
     word, cost shown.
   - Is it pleasant to use? What feels slow or confusing?
4. **Two providers.**
   - Connect a second provider and start several new conversations.
   - Check with the query below that they spread across providers, and that each conversation keeps
     to its own provider.
5. **Long input.** Paste a long document (over 20,000 characters) and ask about it.
   - Expect the powerful tier.
   - Mind the cost: tell Atishay first.
6. **Choose a model.** Try `/models`, then `/model <name>`, `/model powerful` and `/model auto`
   (or the web Model menu).
   - Is the choice obvious? Is the current choice always visible? Does each reply name its model?
7. **Restart.** Stop and start the gateway and both apps.
   - The conversation and its history must still be there.
8. **Failures.**
   - Stop the gateway while an app is open. Expect a plain "Gateway unreachable" message.
   - Disconnect a provider with `/logout`, then send a message to a conversation that used it.
   - Turn Wi-Fi off mid-reply.
   - Every failure must be explained honestly. Nothing may be invented.
9. **Secret hygiene (spec §11.3).**
   - After the journeys, search the gateway log output and `~/.config/enzo` for the start of each key
     Atishay used. Expect no hits.
   - `connections.json` must contain no key.
10. **Usability verdict.** After a session, write 5 to 10 bullets with Atishay:
    - Could he use this every day?
    - What is missing first?
    - What annoyed him?

### Seeing what happened inside

Which model answered, what it cost, and every failed attempt:

```bash
psql -U enzo -d enzo -c "SELECT started_at, conversation_id, brain, model, task, outcome, input_tokens, output_tokens, cost_usd, fallback_from FROM brain_calls ORDER BY started_at DESC LIMIT 20"
```

Accounts (never shows keys):

```bash
curl -s http://127.0.0.1:8787/connections
```

The catalog by tier, and what is available right now:

```bash
curl -s http://127.0.0.1:8787/brains
```

## Reporting

- **One issue per problem** on `Atishay8192261/Enzo`. Use `gh issue create`.
  - Add the label `user-testing`. Create the label if it is missing, after asking once.
- **Title:** what the user sees, e.g. "Terminal: reply stops halfway when Wi-Fi drops".
- **Body:**
  - Steps to reproduce.
  - Expected result, and actual result.
  - Severity: blocker, major or minor.
  - The redacted gateway log lines and the `brain_calls` rows that matter.
  - Versions: `git rev-parse --short HEAD` and the output of `bun --version`.
  - Which spec section it breaks, if any.
- **Ideas and missing features:** a separate issue labelled `idea`. Say who would want it and why.
- **Summarize for Atishay** in a few bullets after each session, linking the issues.

## What is coming

- **Sign in with ChatGPT.** You will run the login on this Mac and help check that his ChatGPT plan
  pays for replies.
- **Tools and connectors** come next (spec §7, §8). Atishay picks the order. After each one lands,
  test it the same way.
- Live checks against the real providers (spec §1.4), once keys are connected here:
  - the same tasks on two different models;
  - a forced fallback;
  - a log line showing provider, model, tokens and latency.
