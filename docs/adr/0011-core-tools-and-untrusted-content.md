# ADR 0011 — Core tools are hand-built; outside content is fenced as untrusted

- **Status:** Accepted (2026-11-18). Decided with Atishay: hand-built core tools, MCP for services.
- **Spec:** §7.2 (tool categories, text-fetch browser mode), §11.2 (user-visible files folder),
  §12.2–12.4 (authority, approvals, injection defence), §10.3 (no tool names in user-facing text)

## Context

Enzo could only chat. Atishay chose a mix: everyday core tools that must be rock-solid are
hand-built; services (GitHub, Gmail, Calendar, …) will plug in through MCP later, behind the same
approval, validation and logging. Outside content (web pages, files) may carry instructions aimed
at the agent.

## Decision

1. **Web search:** Tavily, because it returns clean, ranked text for agents and supports a
   keyless mode (free, rate-limited), so search works with no signup. A free key (1,000
   credits/month, no card) connected via `/login tavily` raises the limits. Only the query is sent.
   Basic depth (1 credit). Verified against docs.tavily.com on 2026-11-18.
2. **Reading pages:** Enzo fetches pages itself, so the URLs it reads are not shared with a third
   party (§12.5). Main text is extracted with trafilatura 2.3. Text types only, 3 MB cap, at most
   5 redirects. A failed fetch says only that this fetch failed (§7.2.4).
3. **SSRF guard:** only `http`/`https`, no logins in URLs, and every address on every redirect hop
   must be public (`ipaddress.is_global`). This refuses localhost, private networks and cloud
   metadata addresses (169.254.169.254). Behind an HTTP proxy, the proxy resolves names, so only
   IP literals and locally resolvable names are checked. DNS rebinding between the check and the
   connection is a known residual risk; pinning the vetted IP would break proxied setups.
4. **Files:** one user-visible folder (`ENZO_WORKSPACE_DIR`, default `~/Enzo`). Paths that leave
   it (`..`, absolute, `~`, symlinks out) are refused. Edits keep the previous version in the hidden
   `.enzo` folder, so they are reversible without approval. Delete needs approval (§12.3) and moves
   the file to `.enzo/trash`. Text files only; writes are atomic.
5. **Untrusted fence:** tools flagged `untrusted_output` have their result wrapped in
   `<untrusted-data source="…" id="X">…</untrusted-data id="X">`. X is 64 random bits per call, so
   content cannot close the fence by guessing it. The system prompt says fenced text is data:
   instructions inside it are reported to the user, never followed.
6. **Plain-language approvals:** an approval-gated tool gives a `summary` of the exact action
   ("Delete old.txt from your Enzo folder (it stays recoverable)"). The card shows it above the
   exact arguments, never a bare tool name (§10.3). The approval id still covers the exact tool
   and arguments.
7. **Service accounts:** accounts that tools use (Tavily) share the model accounts' flow: key
   checked first (Tavily's `/usage`, which costs no credits), then kept in the vault.

## Consequences

- Search, page reading and files work out of the box. Keyless search runs out quickly with heavy
  use (seen in testing); the message says to connect a free key.
- Whether a model actually ignores fenced instructions depends on the model. Enzo guarantees the
  fence and the prompt; live conformance (§1.4, §12.7) must test each model.
- PDFs, JavaScript-only pages and logins need the live-browser mode (§7.2.4), not built yet.
