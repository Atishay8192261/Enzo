"""Sign in with Google endpoints (ADR 0026), the same shape as Sign in with ChatGPT (ADR 0013).

The app asks the gateway to start a sign-in and gets a short local link that forwards the browser
to Google, so the full authorization address never passes through an app or its output. Google
sends the browser back to ``/auth/google/callback`` on this gateway; the app polls the attempt.
The two browser routes carry no bearer token (a browser can't send one): each attempt's random id
and single-use state protect them, and access logs drop their query string.

Without an OAuth client the start answers ``needs_client``: the app then asks for the user's own
client file (docs/connectors/google.md) and sends it to ``PUT /connections/google/client``.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, params
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from enzo.accounts.google import (
    CALLBACK_PATH,
    PERMISSIONS_URL,
    Access,
    ClientFileError,
    GoogleSignIn,
    NoClientError,
)
from enzo.gateway.chatgpt import _NO_STORE, LOOPBACK, _page
from enzo.gateway.connections import AccountsDep

SETUP_DOC = "https://github.com/Atishay8192261/Enzo/blob/main/docs/connectors/google.md"
_LINK = (PERMISSIONS_URL, "See what Enzo may do in your Google Account")


class ClientIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The OAuth client file Google's console downloads (Desktop app type), as text.
    client: str = Field(min_length=2, max_length=8192, repr=False)


class StartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: "full": read, send and edit; "read": read only.
    access: Access = "full"


class StartOut(BaseModel):
    login_id: str | None = None
    #: Open this in the browser: a local address on the gateway that forwards to Google.
    url: str | None = None
    #: No OAuth client yet: ask for the user's own (``message`` says how).
    needs_client: bool = False
    message: str | None = None


class AttemptOut(BaseModel):
    status: Literal["pending", "connected", "failed"]
    message: str | None


def sign_in_of(accounts: AccountsDep) -> GoogleSignIn:
    if accounts.google is None:
        raise HTTPException(503, "Sign in with Google needs the credential vault, which is off")
    return accounts.google


SignInDep = Annotated[GoogleSignIn, Depends(sign_in_of)]


def google_routes(auth: params.Depends, port: int) -> APIRouter:
    router = APIRouter()
    base = f"http://{LOOPBACK}:{port}"

    @router.put("/connections/google/client", dependencies=[auth], status_code=204)
    async def set_client(body: ClientIn, sign_in: SignInDep) -> None:
        try:
            await sign_in.set_client(body.client)
        except ClientFileError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/connections/google/sign-in", dependencies=[auth])
    def start(sign_in: SignInDep, body: StartIn | None = None) -> StartOut:
        try:
            attempt = sign_in.start(base + CALLBACK_PATH, body.access if body else "full")
        except NoClientError:
            return StartOut(
                needs_client=True,
                # The link gets its own line: a terminal must never wrap it into other words.
                message=(
                    "Google needs your own OAuth client first: a one-time setup of about 10 "
                    "minutes. Follow these steps, then paste the client file you downloaded "
                    f"below.\n{SETUP_DOC}"
                ),
            )
        return StartOut(login_id=attempt.id, url=f"{base}/auth/google/{attempt.id}")

    @router.get("/connections/google/sign-in/{login_id}", dependencies=[auth])
    def status(login_id: str, sign_in: SignInDep) -> AttemptOut:
        attempt = sign_in.attempt(login_id)
        if attempt is None:
            raise HTTPException(404, "this sign-in expired: start again with /login google")
        return AttemptOut(status=attempt.status, message=attempt.message)

    # Before the {login_id} route, which would otherwise take "callback" as an id.
    @router.get(CALLBACK_PATH, include_in_schema=False)
    async def callback(request: Request, sign_in: SignInDep) -> HTMLResponse:
        attempt = await sign_in.finish(dict(request.query_params))
        if attempt is None:
            return _page("This sign-in is unknown or expired", "Start again from Enzo.", 400, _LINK)
        if attempt.status == "connected":
            return _page(
                "Signed in to Enzo",
                f"{attempt.message} You can close this tab and go back to Enzo.",
                link=_LINK,
            )
        return _page("Google sign-in did not finish", attempt.message or "Try again.", 400, _LINK)

    @router.get("/auth/google/{login_id}", include_in_schema=False, response_model=None)
    def go(login_id: str, sign_in: SignInDep) -> RedirectResponse | HTMLResponse:
        attempt = sign_in.attempt(login_id)
        if attempt is None or attempt.status != "pending":
            return _page(
                "This sign-in link expired", "Start again from Enzo: /login google.", link=_LINK
            )
        return RedirectResponse(sign_in.authorize_url(attempt), status_code=302, headers=_NO_STORE)

    return router
