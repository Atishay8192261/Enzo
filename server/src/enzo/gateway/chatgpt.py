"""Sign in with ChatGPT endpoints (ADR 0013).

The app asks the gateway to start a sign-in and gets back a short local link. That link sends the
browser on to OpenAI, so the full authorization address never passes through an app or its output.
OpenAI sends the browser back to ``/auth/callback`` on this gateway. The app polls the attempt to
learn how it ended. The two browser routes carry no bearer token: browsers can't send one, and each
attempt's random id and state are what protect them.
"""

import html
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, params
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict

from enzo.accounts.chatgpt import CALLBACK_PATH, USAGE_URL, ChatGPTSignIn
from enzo.gateway.connections import AccountsDep

#: The browser comes back here (only the port may vary in OpenAI's loopback rule).
LOOPBACK = "127.0.0.1"
_NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


class StartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Register another ChatGPT account instead of signing in to the one Enzo knows.
    new_account: bool = False


class StartOut(BaseModel):
    login_id: str
    #: Open this in the browser. A local address on the gateway; it forwards to OpenAI.
    url: str


class AttemptOut(BaseModel):
    status: Literal["pending", "connected", "failed"]
    message: str | None
    #: The first sign-in on this computer: apps say once that replies now use the plan.
    first_time: bool
    #: Where the user reviews and limits what Enzo uses of their plan.
    usage_url: str = USAGE_URL


def sign_in_of(accounts: AccountsDep) -> ChatGPTSignIn:
    if accounts.chatgpt is None:
        raise HTTPException(503, "Sign in with ChatGPT needs the credential vault, which is off")
    return accounts.chatgpt


SignInDep = Annotated[ChatGPTSignIn, Depends(sign_in_of)]


def chatgpt_routes(auth: params.Depends, port: int) -> APIRouter:
    router = APIRouter()
    base = f"http://{LOOPBACK}:{port}"

    @router.post("/connections/openai/chatgpt", dependencies=[auth])
    def start(sign_in: SignInDep, body: StartIn | None = None) -> StartOut:
        attempt = sign_in.start(base + CALLBACK_PATH, new_account=bool(body and body.new_account))
        return StartOut(login_id=attempt.id, url=f"{base}/auth/chatgpt/{attempt.id}")

    @router.get("/connections/openai/chatgpt/{login_id}", dependencies=[auth])
    def status(login_id: str, sign_in: SignInDep) -> AttemptOut:
        attempt = sign_in.attempt(login_id)
        if attempt is None:
            raise HTTPException(404, "this sign-in expired: start again with /login chatgpt")
        return AttemptOut(
            status=attempt.status,
            message=attempt.message,
            first_time=attempt.status == "connected" and attempt.registration is None,
        )

    @router.get("/auth/chatgpt/{login_id}", include_in_schema=False, response_model=None)
    def go(login_id: str, sign_in: SignInDep) -> RedirectResponse | HTMLResponse:
        attempt = sign_in.attempt(login_id)
        if attempt is None or attempt.status != "pending":
            return _page("This sign-in link expired", "Start again from Enzo: /login chatgpt.")
        return RedirectResponse(sign_in.authorize_url(attempt), status_code=302, headers=_NO_STORE)

    @router.get(CALLBACK_PATH, include_in_schema=False)
    async def callback(request: Request, sign_in: SignInDep) -> HTMLResponse:
        attempt = await sign_in.finish(dict(request.query_params))
        if attempt is None:
            return _page("This sign-in is unknown or expired", "Start again from Enzo.", 400)
        if attempt.status == "connected":
            return _page(
                "Signed in to Enzo",
                f"{attempt.message} Enzo's GPT replies now use your ChatGPT plan. You can close "
                "this tab and go back to Enzo.",
            )
        return _page("ChatGPT sign-in did not finish", attempt.message or "Try again.", 400)

    return router


def _page(title: str, text: str, status_code: int = 200) -> HTMLResponse:
    """A plain page for the browser tab: no scripts, no external resources."""
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Enzo</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font: 16px/1.5 system-ui, sans-serif; margin: 0; display: grid; min-height: 100vh;
         place-items: center; background: #faf9f7; color: #1c1b1a; }}
  main {{ max-width: 30rem; padding: 2rem; }}
  h1 {{ font-size: 1.25rem; font-weight: 600; }}
  a {{ color: inherit; }}
  @media (prefers-color-scheme: dark) {{ body {{ background: #161514; color: #ecebe8; }} }}
</style></head>
<body><main><h1>{html.escape(title)}</h1><p>{html.escape(text)}</p>
<p><a href="{USAGE_URL}" rel="noreferrer">Manage your ChatGPT usage</a></p></main></body></html>"""
    return HTMLResponse(body, status_code=status_code, headers=_NO_STORE)


class RedactAuthQuery(logging.Filter):
    """Access logs keep the path of browser sign-in routes but never their query (the one-time
    code and state)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path = args[2]
            if path.startswith("/auth/") and "?" in path:
                record.args = (*args[:2], path.split("?", 1)[0] + "?[redacted]", *args[3:])
        return True
