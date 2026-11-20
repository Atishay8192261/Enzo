# Enzo

**A private, local-first personal AI agent.** Enzo runs on your own computer, uses your own model
accounts (sign in with your ChatGPT plan, or bring a Claude, OpenAI, Grok or Kimi API key), and
keeps your data on your machine. It chats, searches the web, reads pages, works with files in
`~/Enzo` and with GitHub, and asks before anything that can't be undone.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/Atishay8192261/Enzo/main/install.sh | sh
```

Or, if you already use [uv](https://docs.astral.sh/uv/):

```bash
uv tool install enzo-agent
```

Nothing else to install: Python, the database (Postgres with pgvector) and the terminal app come
with it. macOS (Apple silicon or Intel) and Linux (x64 or arm64). On Windows, use WSL.

## Use

```bash
enzo            # the terminal app, in a new chat
enzo -c         # the terminal app, in your last chat (ctrl+r there lists them all)
enzo web        # the web app, in your browser
enzo doctor     # check the installation; each problem comes with a fix
enzo logs       # what Enzo did, one line per step (-f to follow; never what was said)
enzo update     # the latest release
```

In the app, connect a model account first: `/login chatgpt` uses your ChatGPT plan (no API key),
or `/login anthropic`, `/login openai`, `/login xai`, `/login moonshot` with a key. Keys and
sign-ins are kept in your system keychain, never in files or chats.

Enzo remembers what you tell it about yourself across chats (preferences, people, decisions).
`/memory` shows what it remembers and where each fact came from, `/forget` removes one, and
`~/Enzo/Memory.md` is a readable copy. To change a memory, just tell Enzo. It never keeps
passwords, keys, codes or card numbers.

## Where things live

| What | Where |
| --- | --- |
| Your files | `~/Enzo` |
| What Enzo remembers (readable copy) | `~/Enzo/Memory.md` |
| Conversations and memory (database) | `~/.local/share/enzo/postgres` |
| Settings, accounts index | `~/.config/enzo` |
| Log (`enzo logs`) | `~/.local/state/enzo/logs/gateway.log` |

To remove Enzo: `uv tool uninstall enzo-agent`, then delete the folders above if you want your
data gone too.

## Learn more

Source, documentation and issues: <https://github.com/Atishay8192261/Enzo>. Licensed under
Apache-2.0.
