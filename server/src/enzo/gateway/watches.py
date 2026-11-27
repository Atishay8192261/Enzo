"""``/watches``: list, check now (the dry run), turn on with the user's yes, cancel (ADR 0031)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, params, status
from pydantic import BaseModel, Field

from enzo.hooks.sources import PollError
from enzo.hooks.store import HookRefusedError
from enzo.hooks.tools import WatchOut, watch_out
from enzo.hooks.watcher import Watcher


class WatchListOut(BaseModel):
    watches: list[WatchOut]


class CheckOut(BaseModel):
    """A dry run, as the app shows it before asking to turn the watch on."""

    watch: WatchOut
    #: What it reads right now (outside content: show it as text, never as markup).
    reads: str
    #: What counts as a change: "the number in it goes below 40".
    change: str
    checked_at: str | None
    #: Turning it on names this dry run: the one the user saw.
    token: str


class EnableIn(BaseModel):
    token: str = Field(min_length=1, max_length=64)


def watcher(request: Request) -> Watcher:
    bg = getattr(request.app.state, "background", None)
    found: Watcher | None = getattr(bg, "watcher", None)
    if found is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "watches are not running")
    return found


WatcherDep = Annotated[Watcher, Depends(watcher)]


def watch_routes(auth: params.Depends) -> APIRouter:
    router = APIRouter(prefix="/watches", dependencies=[auth])

    @router.get("")
    async def list_watches(w: WatcherDep) -> WatchListOut:
        return WatchListOut(watches=[watch_out(h) for h in await w.hooks.listed()])

    @router.post("/{hook_id}/check")
    async def check(hook_id: int, w: WatcherDep) -> CheckOut:
        """Read it now, record it, and say what counts as a change (spec §9.4 dry run)."""
        try:
            dry = await w.check(hook_id)
        except (PollError, HookRefusedError) as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
        return CheckOut(
            watch=watch_out(dry.hook),
            reads=dry.seen.shown,
            change=dry.hook.change_words,
            checked_at=dry.hook.local(dry.at),
            token=dry.token,
        )

    @router.post("/{hook_id}/enable")
    async def enable(hook_id: int, body: EnableIn, w: WatcherDep, request: Request) -> WatchOut:
        """Turn it on, from the dry run the user saw: the app asked them first."""
        try:
            on = await w.hooks.enable(hook_id, body.token, w.now())
        except HookRefusedError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
        request.app.state.background.runner.poke()  # its first check comes on time
        return watch_out(on)

    @router.delete("/{hook_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def cancel(hook_id: int, w: WatcherDep) -> None:
        try:
            await w.hooks.cancel(hook_id)
        except HookRefusedError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None

    return router
