"""Looking at a kept image again (ADR 0033): after a summary, older images are not sent any more;
the summary lists them by id, and this brings one back for the brain to see."""

import dataclasses
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from enzo.brain.models import IMAGE_ID_PATTERN
from enzo.images.store import ImageStore
from enzo.tools.registry import ImageResult, Tool, tool

TOOL_NAME = "view_image"


class ViewParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        pattern=IMAGE_ID_PATTERN, description="The image's id, e.g. img_3fa2c1d4e5f60718."
    )


class Viewed(ImageResult):
    id: str
    width: int
    height: int
    #: Where it came from: "an image the user pasted", "a screenshot Enzo took".
    source: str


def image_tools(store: ImageStore) -> list[Tool[Any, Any]]:
    @tool(
        name=TOOL_NAME,
        description=(
            "Look again at an image from earlier in this chat by its id (an earlier summary "
            "lists them). The image comes with the result."
        ),
        activity=lambda _a: "Looking at an earlier image",
    )
    async def view_image(params: ViewParams) -> Viewed:
        stored = store.get(params.id)
        if stored is None:
            msg = f"there is no image {params.id}"
            raise LookupError(msg)
        return Viewed(
            id=stored.id,
            width=stored.width,
            height=stored.height,
            source=stored.words,
            images=(stored.part(),),
        )

    return [dataclasses.replace(view_image, needs_vision=True, keep_whole=True)]
