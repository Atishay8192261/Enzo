"""``POST /images``: an image the user attaches is kept first, then sent with a message by id.

The body is the file itself (any image type Enzo reads; its real type comes from its bytes, not
the header). The file's name and how it came (pasted, a file) ride in headers, so neither ever
appears in an access log; the bytes are never logged (ADR 0033).
"""

from typing import Annotated, Literal
from urllib.parse import unquote

from fastapi import APIRouter, Header, HTTPException, Request, params
from pydantic import BaseModel

from enzo.gateway.conversations import ServiceDep
from enzo.images.store import MAX_BYTES, ImageError


class ImageOut(BaseModel):
    id: str
    media_type: str
    width: int
    height: int
    bytes: int
    name: str | None
    #: What was removed before it was kept: "EXIF", "GPS position", ...
    stripped: list[str]


def image_routes(auth: params.Depends) -> APIRouter:
    router = APIRouter(prefix="/images", dependencies=[auth])

    @router.post("", status_code=201)
    async def keep_image(
        request: Request,
        s: ServiceDep,
        x_enzo_image_source: Annotated[Literal["pasted", "file"], Header()] = "pasted",
        x_enzo_image_name: Annotated[str | None, Header(max_length=1000)] = None,
    ) -> ImageOut:
        if s.images is None:
            raise HTTPException(503, "Images are not set up in this Enzo.")
        data = await _body(request)
        try:
            kept = await s.images.keep_async(
                data,
                source=x_enzo_image_source,
                name=unquote(x_enzo_image_name) if x_enzo_image_name else None,
            )
        except ImageError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        return ImageOut(
            id=kept.id,
            media_type=kept.media_type,
            width=kept.width,
            height=kept.height,
            bytes=kept.bytes,
            name=kept.name,
            stripped=list(kept.stripped),
        )

    return router


async def _body(request: Request) -> bytes:
    """The upload, refused as soon as it passes the limit (never read whole into memory)."""
    too_big = HTTPException(413, f"That image is too big: the most is {MAX_BYTES // 2**20} MB.")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BYTES:
        raise too_big
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BYTES:
            raise too_big
        chunks.append(chunk)
    return b"".join(chunks)
