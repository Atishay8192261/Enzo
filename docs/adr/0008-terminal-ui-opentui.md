# ADR 0008 — Terminal UI on OpenTUI

- **Status:** Accepted (decided by Atishay Jain, 2026-11-17)
- **Supersedes:** the Ink choice in ADR 0001

## Context

Checked what current coding agents use: Claude Code and Gemini CLI build on Ink (Claude Code on a
heavily customized fork), OpenAI's Codex CLI moved to Rust + Ratatui, and OpenCode moved from Go's
Bubble Tea to OpenTUI (TypeScript/React on a native Zig core). Atishay chose OpenTUI.

## Decision

`clients/cli` renders with `@opentui/react` + `@opentui/core` 0.9.2, pinned to the exact version, and
runs on Bun 1.4.4 (OpenTUI needs Bun 1.3.14+ or Node 26.4+). Chat state stays in `@enzo/chat`, shared
with the web app, so only the screen code changed.

## Consequences

- Users need Bun to run the terminal app. CI installs it with `oven-sh/setup-bun` (pinned SHA).
- OpenTUI is pre-1.0: updates are deliberate, one version at a time, with tests and the real-terminal
  smoke test.
- OpenTUI's input and key parsing handle fast typing and pastes; tests cover the two input bugs found
  under Ink (dropped first key, Enter lost in a fast chunk).
