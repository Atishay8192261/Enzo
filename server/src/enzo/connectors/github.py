"""GitHub connector (issue #2, spec §8): GitHub's hosted MCP server, plus two small REST tools.

Setup and operations: docs/connectors/github.md. Auth: a fine-grained personal access token the
user connects with ``/login github``; it is checked against ``GET /user`` and kept in the vault.

- MCP: GitHub's hosted server (no install). Only the toolsets Enzo needs, lockdown mode on
  (hides public issue text from people without push access, a best-effort injection filter),
  and an allowlist of tools.
- Merging is made exact before the user is asked: Enzo reads the pull request, pins the merge to
  its current head commit (``expectedHeadSha``) and shows the diff. If the PR changes before the
  approval, Enzo asks again; if it changes after, GitHub refuses the merge.
- Deleting a branch and creating a release are not in the MCP server, so they are two direct
  REST calls with the same token. Both need approval; the default branch is never deleted.
"""

import json
from collections.abc import Callable
from typing import Any

import httpx2
import mcp_types
from mcp import Client
from pydantic import BaseModel, ConfigDict, Field

from enzo import __version__
from enzo.brain.errors import (
    AuthError,
    ProviderConnectionError,
    ProviderServerError,
    ProviderTimeoutError,
    RateLimitError,
)
from enzo.connectors.mcp import (
    CallFn,
    ClientFactory,
    ConnectorError,
    McpConnector,
    McpServer,
    http_transport,
)
from enzo.tools.registry import Prepared, Tool, tool

MCP_URL = "https://api.githubcopilot.com/mcp/"
REST = "https://api.github.com"
#: The version GitHub selects by default (checked 2026-11-18 via X-GitHub-Api-Version-Selected).
API_VERSION = "2022-11-28"
MAX_DIFF_CHARS = 20_000

#: Non-secret MCP headers (docs: github/github-mcp-server docs/remote-server.md, read 2026-11-18).
MCP_HEADERS = {
    "X-MCP-Toolsets": "context,repos,issues,pull_requests,actions",
    "X-MCP-Lockdown": "true",
}

#: What Enzo exposes (issue #2): read and write repos, issues, PRs, CI. Nothing that deletes a
#: repository or files, forks, or changes settings.
ALLOWED = frozenset(
    {
        "get_me",
        "get_file_contents",
        "list_branches",
        "create_branch",
        "create_or_update_file",
        "push_files",
        "list_commits",
        "get_commit",
        "create_repository",
        "list_issues",
        "issue_read",
        "issue_write",
        "search_issues",
        "add_issue_comment",
        "list_pull_requests",
        "pull_request_read",
        "create_pull_request",
        "update_pull_request",
        "merge_pull_request",
        "add_reply_to_pull_request_comment",
        "actions_list",
        "actions_get",
        "get_job_logs",
        "list_releases",
        "list_tags",
    }
)


async def pin_merge(args: dict[str, Any], call: CallFn) -> tuple[dict[str, Any], str | None]:
    """Pin a merge to the PR's current head and show what it lands (§12.3)."""
    where = {"owner": args["owner"], "repo": args["repo"], "pullNumber": args["pullNumber"]}
    raw = await call("pull_request_read", {"method": "get", **where})
    try:
        pr = json.loads(raw)
        head = pr["head"]["sha"]
        title = pr.get("title", "")
    except (ValueError, KeyError, TypeError) as exc:
        msg = "could not read the pull request's head commit, so the merge was not prepared"
        raise ConnectorError(msg) from exc
    asked = args.get("expectedHeadSha")
    if asked and asked != head:
        msg = f"the pull request changed (head is now {head[:12]}, not {str(asked)[:12]})"
        raise ConnectorError(msg)
    diff = await call("pull_request_read", {"method": "get_diff", **where})
    cut = len(diff) > MAX_DIFF_CHARS
    detail = (
        f'Merging "{title}" at commit {head[:12]} into its base.\n\n'
        f"{diff[:MAX_DIFF_CHARS]}"
        + (
            f"\n… diff cut at {MAX_DIFF_CHARS} characters; the merge includes all of it."
            if cut
            else ""
        )
    )
    return {**args, "expectedHeadSha": head}, detail


def github_server() -> McpServer:
    return McpServer(
        name="github", label="GitHub", allow=ALLOWED, prepare={"merge_pull_request": pin_merge}
    )


def mcp_headers(token: str) -> dict[str, str]:
    return {**MCP_HEADERS, "Authorization": f"Bearer {token}"}


async def github_tools(
    token: Callable[[], str | None],
    http: httpx2.AsyncClient,
    *,
    connect: ClientFactory | None = None,
) -> list[Tool[Any, Any]]:
    """All GitHub tools for the connected token, or none when GitHub is not connected."""
    key = token()
    if not key:
        return []
    info = mcp_types.Implementation(name="enzo", version=__version__)
    factory = connect or (
        lambda: Client(http_transport(MCP_URL, mcp_headers(key)), client_info=info)
    )
    return [*await McpConnector(github_server(), factory).tools(), *rest_tools(token, http)]


async def check_github_token(
    api_key: str, *, http_client: httpx2.AsyncClient | None = None
) -> None:
    """Who the token belongs to: read-only, and fails exactly like the real calls would."""
    ctx = {"provider": "github", "model": "-"}
    try:
        response = await _request(http_client, api_key, "GET", "/user")
    except httpx2.TimeoutException as exc:
        raise ProviderTimeoutError("GitHub did not answer in time", **ctx) from exc
    except httpx2.HTTPError as exc:
        raise ProviderConnectionError(
            f"could not reach GitHub ({type(exc).__name__})", **ctx
        ) from exc
    if response.status_code in (401, 403):
        if response.headers.get("x-ratelimit-remaining") == "0":
            raise RateLimitError("GitHub is rate limiting this token", **ctx)
        raise AuthError("GitHub rejected this token", **ctx)
    if response.status_code == 429:
        raise RateLimitError("GitHub is rate limiting this token", **ctx)
    if response.status_code != 200:
        raise ProviderServerError(f"GitHub answered HTTP {response.status_code}", **ctx)


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BranchParams(_Params):
    owner: str = Field(min_length=1, description="Repository owner.")
    repo: str = Field(min_length=1, description="Repository name.")
    branch: str = Field(min_length=1, description="Branch to delete (never the default branch).")
    #: Filled in by Enzo before asking: the commit the branch points at.
    expected_sha: str | None = Field(default=None, description="Leave empty; Enzo fills it in.")


class Done(BaseModel):
    done: str
    url: str | None = None


class ReleaseParams(_Params):
    owner: str = Field(min_length=1, description="Repository owner.")
    repo: str = Field(min_length=1, description="Repository name.")
    tag: str = Field(min_length=1, description="Tag to create, e.g. v1.2.0.")
    target: str | None = Field(
        default=None, description="Branch or commit (default branch if empty)."
    )
    title: str | None = Field(default=None, description="Release title (the tag if empty).")
    notes: str = Field(default="", description="Release notes (Markdown).")
    draft: bool = Field(default=False, description="Create as a draft.")
    prerelease: bool = Field(default=False, description="Mark as a pre-release.")


def rest_tools(token: Callable[[], str | None], http: httpx2.AsyncClient) -> list[Tool[Any, Any]]:
    """The two GitHub actions the MCP server lacks, as direct REST calls (issue #2)."""

    async def pin_branch(params: BranchParams) -> Prepared[BranchParams]:
        where = f"{params.owner}/{params.repo}"
        repo = await _json(http, token, "GET", f"/repos/{where}")
        if repo.get("default_branch") == params.branch:
            msg = f"{params.branch} is the default branch of {where}; Enzo never deletes it"
            raise ConnectorError(msg)
        sha = await _branch_head(http, token, params)
        if params.expected_sha and params.expected_sha != sha:
            msg = f"the branch moved (now at {sha[:12]})"
            raise ConnectorError(msg)
        detail = f"Deletes branch {params.branch} of {where}, now at commit {sha[:12]}."
        return Prepared(params.model_copy(update={"expected_sha": sha}), detail)

    @tool(
        name="github_delete_branch",
        description="GitHub: delete a branch, e.g. after its pull request merged. Needs approval.",
        requires_approval=True,
        untrusted_output=True,
        summary=_delete_summary,
        prepare=pin_branch,
    )
    async def delete_branch(params: BranchParams) -> Done:
        if await _branch_head(http, token, params) != params.expected_sha:
            msg = "the branch moved after you approved, so it was not deleted; ask again"
            raise ConnectorError(msg)
        path = f"/repos/{params.owner}/{params.repo}/git/refs/heads/{params.branch}"
        await _json(http, token, "DELETE", path)
        return Done(done=f"deleted branch {params.branch}")

    @tool(
        name="github_create_release",
        description="GitHub: create a release and its tag. Publishes it publicly. Needs approval.",
        requires_approval=True,
        untrusted_output=True,
        summary=_release_summary,
    )
    async def create_release(params: ReleaseParams) -> Done:
        body: dict[str, Any] = {
            "tag_name": params.tag,
            "name": params.title or params.tag,
            "body": params.notes,
            "draft": params.draft,
            "prerelease": params.prerelease,
        }
        if params.target:
            body["target_commitish"] = params.target
        path = f"/repos/{params.owner}/{params.repo}/releases"
        made = await _json(http, token, "POST", path, body)
        return Done(done=f"created release {params.tag}", url=made.get("html_url"))

    return [delete_branch, create_release]


def _delete_summary(params: BranchParams) -> str:
    return f"GitHub: delete branch {params.branch} ({params.owner}/{params.repo})"


def _release_summary(params: ReleaseParams) -> str:
    return f"GitHub: publish release {params.tag} ({params.owner}/{params.repo})"


async def _branch_head(
    http: httpx2.AsyncClient, token: Callable[[], str | None], params: BranchParams
) -> str:
    path = f"/repos/{params.owner}/{params.repo}/git/ref/heads/{params.branch}"
    ref = await _json(http, token, "GET", path)
    sha: str = ref["object"]["sha"]
    return sha


async def _request(
    http: httpx2.AsyncClient | None,
    token: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> httpx2.Response:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
    }
    if http is None:
        async with httpx2.AsyncClient(timeout=20) as own:
            return await own.request(method, REST + path, headers=headers, json=body)
    return await http.request(method, REST + path, headers=headers, json=body)


async def _json(
    http: httpx2.AsyncClient,
    token: Callable[[], str | None],
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    key = token()
    if not key:
        msg = "GitHub is not connected: run /login github"
        raise ConnectorError(msg)
    try:
        response = await _request(http, key, method, path, body)
    except httpx2.HTTPError as exc:
        msg = f"could not reach GitHub ({type(exc).__name__})"
        raise ConnectorError(msg) from exc
    if response.status_code == 204:
        return {}
    if response.status_code in (401, 403) and response.headers.get("x-ratelimit-remaining") != "0":
        msg = "GitHub rejected the token: reconnect with /login github"
        raise ConnectorError(msg)
    if response.status_code in (403, 429):
        msg = "GitHub is rate limiting Enzo: stopped, nothing was retried"
        raise ConnectorError(msg)
    if response.status_code >= 400:
        msg = f"GitHub answered HTTP {response.status_code} for {method} {path}"
        raise ConnectorError(msg)
    data = response.json()
    return data if isinstance(data, dict) else {"items": data}
