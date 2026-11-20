# ADR 0017 — The terminal app: Enzo's own full-screen layout, live steps, and usage everywhere

- **Status:** Accepted (2026-11-20).
- **Atishay:**
  - Make the terminal app OpenCode-style, but better, and not a copy of its look.
  - Show token usage and everything else for every chat that is opened.
  - Quitting needs a keyword plus a key binding.
- **Issues:** #35 (layout, with its comments), #34 (slash commands), #36 (rendering), #40 (action
  words), #44 (gateway down), #49 (facts behind each message). Amends ADR 0008 (OpenTUI).
- **Spec:** §10.3 (no tool names in chat), §15.4 (tool-call transparency), §1.3.5 (unknown cost is
  never 0), §12.3 (exact approvals).

## Decision

1. **Layout.** OpenCode's structure, drawn Enzo's way:
   - **Opening screen:** the ENZO wordmark (OpenTUI `ascii-font`, block font), the version, the
     account, key hints, and the input under it. Below 60 columns or 20 rows it shows a plain
     title.
   - **Chat:** a header with Enzo, the chat's title and its connection, then the transcript, then
     the input.
     - Your messages are rounded cards titled with the time sent.
     - Enzo's turn opens with "◆ enzo". Replies are Markdown (OpenTUI `markdown`, tree-sitter
       for code blocks).
     - Steps hang on a thin timeline ("│ ✓ …"). A dim facts line sits under each reply.
   - **Around the chat:** a one-line status bar (model, account, what the chat used, keys). A
     side panel at 120+ columns (`/sidebar`) with the full usage and the accounts.
   - **Lists:** a slash popup with argument completion; a Ctrl+P palette; pickers for model and
     accounts. All are type-to-filter, ↑/↓, Enter, Esc.
   - **Command output** is a card titled with the command. A card's title shows arguments only
     when the command takes them as typed, so a key pasted after `/login xai` is never drawn.
   - **Colours:** no background fills (light and dark terminals both work). Dialogs take a panel
     colour from the terminal's reported mode.
2. **Steps in plain words (#40):**
   - Each tool can declare `activity(args) -> str` ("Reading plan.md"). Otherwise its approval
     `summary` is used (so MCP tools read "GitHub: Merge pull request (…)"), then "Working on a
     step".
   - The registry makes the words safe: one printable line, at most 90 characters. Arguments come
     from the brain, so escape sequences must not reach the terminal.
   - `tool_started`/`tool_finished` carry `label`, and stored tool calls carry `label` and
     `latency_ms`, so a reopened chat shows its steps too. Tool names never reach the screen.
3. **Usage everywhere (#49):**
   - `done` carries `usage`: tokens in/out/cached, calls, cost or plan, models, model time.
     `GET /conversations/{id}/usage` gives the chat's total and one usage per user message.
   - **Grouping by turn:** a turn's messages are saved in one transaction and share its `now()`.
     Each brain call belongs to the first save written after it started. A save that holds a user
     message opens a turn; a resume after an approval continues it.
   - Live replies also show the time to the first word and the total wait (client clock). A
     reopened chat shows the models' own time instead. `/details off` hides the per-message
     facts; the status bar keeps the totals.
4. **Enzo unreachable (#44):**
   - The SDK raises `GatewayUnreachableError` when a request cannot connect at all; a request the
     caller stopped stays an abort.
   - The shared chat checks `/healthz` every 3 s. It marks the chat offline and back, connects on
     the first success after starting offline, and offers `/retry` for the message that did not
     go out. Each surface words the fix.
5. **Keys:**
   - Enter sends. A new line: Shift+Enter where the terminal reports it, Alt+Enter, and Ctrl+J
     everywhere. OpenTUI requests the kitty keyboard protocol by default, so Shift+Enter works in
     Kitty, Ghostty, WezTerm and iTerm2.
   - ↑/↓ recall this chat's messages. Long pastes become "[Pasted N lines]" tags, expanded in one
     pass when sent.
   - Text typed while a reply streams is kept, never sent and never lost.
   - Mouse selection, and Ctrl+C with a selection, copy through the system clipboard and OSC 52.
   - Ctrl+C clears the input, or quits when it is empty. **Quit: `exit`/`quit` typed alone, or
     Ctrl+D.**

## Consequences

- One look across surfaces is still to come: the web app shows the step lines, but its Markdown
  and usage details wait for its own pass (#37 is parked).
- Per-message facts for history depend on the save-timestamp grouping. A future per-turn id in
  the ledger (#43) would make it exact rather than inferred.
- OpenTUI's test renderer ignores flex gaps (a real terminal draws them), so tests check content
  and order, not blank lines. The live pty check (`scripts/tty-smoke.py` style, read through
  pyte) covers the real drawing.
- Images in the input (#35's lower-priority part) are not in this change.
