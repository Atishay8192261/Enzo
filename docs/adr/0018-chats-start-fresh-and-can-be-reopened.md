# ADR 0018 — Chats start fresh, are created by their first message, and can be reopened

- **Status:** Accepted (2026-11-20).
- **Issues:** #57 (the opening screen flashed, then every window jumped into the same last chat)
  and #38 (no way to list or reopen past chats). Atishay: pair them, ship them in v0.1.1.
- **Spec:** §10.2 (main chat and side chats; old work must stay reachable), §11.2 (the transcript
  is kept).

## Decision

1. **Start:** `enzo` opens a new chat; `enzo -c` / `--continue` reopens the last one. This
   follows Claude Code (`claude` / `--continue` / `/resume`) and Codex (`resume`).
2. **Created by the first message:**
   - A fresh chat has no id until its first message is sent. The gateway then creates it, with
     any model or tier picked before.
   - So `enzo` draws the opening screen once and keeps it, never jumping into old work.
   - Two windows are two chats.
   - Quitting an untouched chat leaves nothing behind: no empty chats in the list.
   - The web app keeps reopening its last chat until its own pass (#37); `useChat` takes
     `start: "last" | "fresh"`.
3. **`--continue` draws no opening screen.** It waits for its chat instead, so nothing flashes.
4. **The list:**
   - `GET /conversations` lists chats that have at least one message, most recently active first
     (by their last saved message). It gives each one's title, when it was last active and how
     many messages the user sent.
   - The terminal opens it with Ctrl+R or `/chats` (alias `/resume`): type to filter, Enter
     reopens.
   - The opening screen offers the newest chat.
5. **Titles:**
   - A chat is named after the first line of its first message, made printable and cut to 60
     characters. A model-written title can come later (it would cost a call per chat).
   - `PATCH /conversations/{id}` renames it (`/rename`). A rename is never overwritten.
   - Migration 0004 names chats saved before this.
6. **Trash, not deletion:**
   - `DELETE /conversations/{id}` sets `archived_at`: the chat leaves the list, but stays in the
     database and can still be opened by its id (a window that has it open keeps working).
   - `/delete` asks first, with "No, keep it" as the first choice.

## Consequences

- There is no `/trash` view to restore a chat yet. The data is kept, so it can come later.
- "Also open in another window" is not shown. With fresh starts, two windows share a chat only
  when the user opens the same one on purpose.
- Search covers titles only, not the messages inside chats (later, with memory, spec §6).
