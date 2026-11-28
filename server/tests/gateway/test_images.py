"""Images end to end through the gateway (ADR 0033): kept clean, sent only to brains that see."""

import base64
import io
import json
import os
from pathlib import Path
from typing import Any

import httpx2
from PIL import Image

from enzo.agent.service import ConversationService
from enzo.brain import Message
from enzo.brain.config import BrainConfig, BrainsConfig
from enzo.brain.fake import ScriptedBrain
from enzo.brain.models import ImagePart
from enzo.brain.provider import BrainProvider
from enzo.brain.router import Router
from enzo.images import ImageStore
from enzo.store.repository import Store
from tests.agent import tools as t
from tests.gateway.test_api import client, events, new_conversation
from tests.images.pictures import MAKER, photo

VISION = {"max_edge": 1024, "patch": 32, "max_patches": 1024, "detail": "high", "source": "t"}


def config(*, sees: bool = True) -> BrainsConfig:
    brain: dict[str, Any] = {"api": "anthropic", "api_key_env": "KEY", "tier": "balanced"}
    if sees:
        brain["vision"] = "small"
    return BrainsConfig.model_validate(
        {"brains": {"main": {**brain, "model": "m-main"}}, "vision": {"small": VISION}}
    )


def service(
    store: Store, brain: ScriptedBrain, images: ImageStore, *, sees: bool = True
) -> ConversationService:
    def build(_c: BrainConfig) -> BrainProvider:
        return brain

    router = Router(config(sees=sees), build=build, available=lambda _c: True, images=images)
    return ConversationService(store, router, t.registry(), images=images)


async def upload(c: httpx2.AsyncClient, data: bytes, **headers: str) -> httpx2.Response:
    return await c.post("/images", content=data, headers={"content-type": "image/jpeg", **headers})


async def test_an_attached_photo_reaches_the_brain_downscaled_and_without_exif(
    store: Store, tmp_path: Path
) -> None:
    brain = ScriptedBrain([Message.assistant("A blue picture with white stripes.")])
    async with client(service(store, brain, ImageStore(tmp_path))) as c:
        kept = await upload(
            c, photo(), **{"x-enzo-image-source": "file", "x-enzo-image-name": "IMG%201.jpg"}
        )
        assert kept.status_code == 201
        image = kept.json()
        assert (image["width"], image["height"], image["name"]) == (3000, 2000, "IMG 1.jpg")
        assert image["stripped"] == ["EXIF", "GPS position"]
        cid = await new_conversation(c)
        r = await c.post(
            f"/conversations/{cid}/turns",
            json={"message": "what is this?", "images": [image["id"]]},
        )
        assert events(r.text)[-1][1]["text"] == "A blue picture with white stripes."
        sent = brain.calls[0].messages[-1]
        part = sent.parts[1]
        assert isinstance(part, ImagePart)
        assert part.data is not None
        assert (part.width, part.height, part.detail) == (1024, 683, "high")
        data = base64.b64decode(part.data)
        assert MAKER.encode() not in data
        with Image.open(io.BytesIO(data)) as seen:
            assert seen.size == (1024, 683)
            assert len(seen.getexif()) == 0
        # The transcript keeps the image by id, never its bytes.
        shown = (await c.get(f"/conversations/{cid}/messages")).json()[0]["parts"]
        assert shown[1] == {
            "type": "image",
            "id": image["id"],
            "media_type": "image/jpeg",
            "width": 3000,
            "height": 2000,
        }
        rows = json.dumps(shown)
        assert "data" not in rows


async def test_a_model_that_cannot_see_is_said_before_sending_and_nothing_is_saved(
    store: Store, tmp_path: Path
) -> None:
    brain = ScriptedBrain([])
    async with client(service(store, brain, ImageStore(tmp_path), sees=False)) as c:
        image = (await upload(c, photo(300, 200))).json()
        cid = await new_conversation(c)
        r = await c.post(
            f"/conversations/{cid}/turns", json={"message": "look", "images": [image["id"]]}
        )
        assert r.status_code == 422
        assert "None of your connected models can see images" in r.json()["detail"]
        assert brain.calls == []
        assert (await c.get(f"/conversations/{cid}/messages")).json() == []


async def test_an_image_alone_is_a_message_and_bad_ids_are_refused(
    store: Store, tmp_path: Path
) -> None:
    brain = ScriptedBrain([Message.assistant("A picture.")])
    async with client(service(store, brain, ImageStore(tmp_path))) as c:
        image = (await upload(c, photo(300, 200))).json()
        cid = await new_conversation(c)
        missing = await c.post(
            f"/conversations/{cid}/turns", json={"images": ["img_0123456789abcdef"]}
        )
        assert missing.status_code == 422
        assert "no longer available" in missing.json()["detail"]
        empty = await c.post(f"/conversations/{cid}/turns", json={"message": " "})
        assert empty.status_code == 422
        ok = await c.post(f"/conversations/{cid}/turns", json={"images": [image["id"]]})
        assert events(ok.text)[-1][1]["text"] == "A picture."


async def test_uploads_that_are_not_images_or_too_big_are_refused_plainly(
    store: Store, tmp_path: Path
) -> None:
    async with client(service(store, ScriptedBrain([]), ImageStore(tmp_path))) as c:
        bad = await upload(c, b"%PDF-1.7 not an image")
        assert (bad.status_code, bad.json()["detail"]) == (
            415,
            "That file isn't an image Enzo can read: use PNG, JPEG, GIF, WebP or BMP.",
        )
        big = await upload(c, b"\0" * (20 * 1024 * 1024 + 1))
        assert big.status_code == 413
    assert not os.listdir(tmp_path)  # nothing was kept
