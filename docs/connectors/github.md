# GitHub connector: setup and operations

Spec §8.2 asks for this document before the connector is used. Design: ADR 0012. Issue #2.

## What Enzo can do on GitHub

- **Read:** your profile, files, branches, commits, issues, pull requests (details, diff, files,
  reviews, comments, CI check runs), Actions runs and job logs, releases and tags.
- **Write, each one after your approval:**
  - create a repository or a branch; commit files (create or update, or push several);
  - open, edit or comment on issues and pull requests; reply to review comments;
  - merge a pull request;
  - delete a merged branch; publish a release.
- **Never:** delete repositories or files, fork, or change settings. Those tools are not exposed.

## Setup

1. Create a **fine-grained personal access token** at github.com → Settings → Developer settings →
   Personal access tokens → Fine-grained tokens.
   - **Repositories:** only the ones Enzo should touch.
   - **Permissions (least privilege):**
     - Contents: read and write (branches, commits, releases)
     - Pull requests: read and write
     - Issues: read and write
     - Actions: read
     - Metadata: read (always on)
2. Connect it: `/login github` in the terminal app (hidden prompt), or **Accounts → github** on the
   web. Enzo checks it with `GET /user` first, then keeps it in your keychain (or the encrypted
   vault). It never appears in chats or logs.
3. The GitHub tools appear right away. Disconnecting (`/logout github`) removes them.

## How it works

- Tools come from **GitHub's hosted MCP server** (`https://api.githubcopilot.com/mcp/`), so there
  is nothing to install.
  - Only the `context`, `repos`, `issues`, `pull_requests` and `actions` toolsets are enabled,
    then narrowed by Enzo's allowlist (`enzo.connectors.github.ALLOWED`).
  - **Lockdown mode** is on: GitHub hides public issue text written by people without push access.
    It is a best-effort filter, not a security boundary.
- **Deleting a branch** and **creating a release** are not in the MCP server. They are two direct
  REST calls (`api.github.com`, API version `2022-11-28`) with the same token.
- **Approvals (§12.3):** any tool GitHub does not mark read-only waits for your approval of the
  exact arguments.
  - **Merges** are pinned to the pull request's current head commit, and the card shows the diff.
    If the PR changes before you approve, Enzo asks again; if it changes after, GitHub refuses the
    merge.
  - **Branch deletes** are pinned to the branch's current commit, re-checked just before deleting,
    and never touch the default branch.
- **Untrusted content (§12.4):** issue bodies, comments, diffs and CI logs reach the model fenced
  as data. Instructions inside them are reported to you, never followed.

## Presentation

Link what Enzo did or read (`owner/repo#number`, or the pull request or release URL) so you can
check it. Show diffs and logs only when asked, or on an approval card.

## Health check and failures (§8.2, §8.3)

| What happens | What Enzo does |
| --- | --- |
| `/check github` | Calls `GET /user`. "The key works", or the reason it failed |
| Token rejected (401, bad credentials) | Stops all GitHub actions and says to reconnect with `/login github` |
| Rate limited | Stops all GitHub actions for 15 minutes. Nothing is retried; it reports what was done |
| GitHub's MCP server unreachable at startup | The gateway starts without GitHub tools and logs `connector unavailable` |
| A merge or branch delete cannot be checked | Nothing is asked and nothing runs; the brain is told why |
