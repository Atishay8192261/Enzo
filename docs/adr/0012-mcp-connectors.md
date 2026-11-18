# ADR 0012 — Connectors come from MCP servers, held to Enzo's own tool rules

- **Status:** Accepted (2026-11-18). Decided with Atishay: hand-built core tools, services via MCP.
- **Spec:** §7.2.3 (connectors), §8 (setup doc, auth, status check, rate limits), §12.3–12.4
- **First connector:** GitHub (issue #2), `docs/connectors/github.md`

## Context

Services (GitHub, Gmail, Calendar, …) each have dozens of operations. Writing every one by hand is
slow. MCP servers publish them as tools with a JSON Schema each, often maintained by the service
itself. They must not weaken Enzo's guarantees: validation before running, approval of the exact
action, untrusted outside content, honest failures, a trace of every call.

## Decision

1. **Client:** the official `mcp` SDK (2.3.0), Streamable HTTP for remote servers, with Enzo's own
   `httpx2` client carrying the headers. In-process servers are used in tests. A new session per
   call: simple, nothing long-lived to supervise; the cost is a handshake per call.
2. **Every MCP tool is an Enzo tool:**
   - Named `<connector>_<tool>`, and only tools on the connector's allowlist are exposed.
   - The server's JSON Schema is what the brain sees and what `jsonschema` checks before the call
     runs (`ToolRegistry` supports schema tools next to Pydantic ones).
   - Results are always fenced as untrusted.
   - Every call goes through the same execute path and ledger.
3. **Approval by default:** a tool needs approval unless the server marks it `readOnlyHint`.
   Unknown means "ask".
4. **Prepare before asking:** a tool can make a call exact before the user sees it, and attach a
   detail for the card.
   - GitHub merges are pinned to the head commit (`expectedHeadSha`) and show the diff.
   - The approval id covers the prepared arguments, so if the target changed, the user is asked
     again.
   - If preparing fails, nothing is asked and nothing runs.
5. **§8.3:**
   - A rate limit stops the whole connector for 15 minutes, with no retry.
   - A rejected token stops it until the user reconnects.
   - Both are detected from error phrases ("rate limit", "bad credentials"), never from bare
     numbers.
6. **Accounts:** a connector's token is a service account (vault, check before save, `/login`).
   Its tools are swapped in and out of the registry as the account connects or disconnects, and an
   unreachable server never stops the gateway.
7. **Gaps filled by hand:** when the server lacks an operation the spec needs, a small typed tool
   calls the service's API directly, under the same rules. For GitHub: delete a branch, create a
   release.

## Consequences

- New services are mostly configuration: a server URL, an allowlist, plain summaries, and any
  prepare steps.
- Tool count grows the prompt. Allowlists keep it small; loading only the tools a task needs
  (§3 step 2) is a later improvement.
- The server's own output format matters to prepare steps (the GitHub PR JSON shape). They fail
  closed: if the shape changes, merges stop being offered, not silently mis-pinned.
