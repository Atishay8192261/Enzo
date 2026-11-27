"""Routing a prompt that has an image (ADR 0033): only to brains that can see it, never dropped."""

from pathlib import Path
from typing import Any

import pytest

from enzo.brain import Message, TextPart
from enzo.brain.config import BrainsConfig
from enzo.brain.fake import ScriptedBrain
from enzo.brain.models import ImagePart
from enzo.brain.router import CantSeeImagesError, NoBrainAvailableError, Router
from enzo.brain.tokens import estimate_tokens
from enzo.images import ImageStore
from enzo.images.sizing import image_tokens
from tests.brain.test_router import brain_cfg, ok, router
from tests.images.pictures import screenshot

VISION = {
    "big": {"max_edge": 2576, "patch": 28, "max_patches": 4784, "source": "t"},
    "small": {"max_edge": 1024, "patch": 32, "max_patches": 1024, "detail": "high", "source": "t"},
}


def cfg(**sees: str | None) -> BrainsConfig:
    """``seer`` (anthropic) and ``blind`` (another provider), both powerful; ``sees``: profiles."""
    brains: dict[str, Any] = {
        "seer": brain_cfg("m-seer", recommended=True),
        "blind": brain_cfg("m-blind", provider="other", recommended=True),
    }
    for name, profile in {"seer": "big", **sees}.items():
        if profile:
            brains[name]["vision"] = profile
    return BrainsConfig.model_validate({"brains": brains, "vision": VISION})


def with_image(store: ImageStore, size: tuple[int, int] = (3840, 2160)) -> list[Message]:
    kept = store.keep(screenshot(*size), source="pasted")
    return [Message(role="user", parts=(TextPart(text="what is this?"), kept.part()))]


def test_an_image_routes_only_to_brains_that_can_see() -> None:
    r = router({}, cfg())
    # Without an image either provider may be first; with one, only the seeing brain, no blind
    # fallback.
    for conversation in ("a", "b", "c", "d"):
        plan = r.plan("hard", affinity=conversation, vision=True)
        assert (plan.primary, plan.fallback) == ("seer", None)
    assert {r.plan("hard", affinity=c).primary for c in "abcdefgh"} == {"seer", "blind"}


def test_a_pinned_brain_that_cannot_see_says_so_and_names_one_that_can() -> None:
    r = router({}, cfg())
    with pytest.raises(CantSeeImagesError, match=r"m-blind can't see images.*/model.*seer"):
        r.plan("hard", override="blind", vision=True)


def test_no_connected_brain_that_can_see_is_said_plainly() -> None:
    r = router({}, cfg(), connected={"other"})
    with pytest.raises(CantSeeImagesError, match="None of your connected models can see images"):
        r.plan("hard", vision=True)
    with pytest.raises(NoBrainAvailableError):  # nothing connected at all is its own message
        router({}, cfg(), connected=set()).plan("hard", vision=True)


async def test_each_brain_is_sent_its_own_copy_and_a_blind_one_nothing(tmp_path: Path) -> None:
    store = ImageStore(tmp_path)
    seer, blind = ScriptedBrain([Message.assistant("a red box")]), ok()
    r: Router = router({"m-seer": seer, "m-blind": blind}, cfg(blind="small"))
    r.images = store
    messages = with_image(store)
    await r.chat("hard", messages, max_output_tokens=100, override="seer")
    await r.chat("hard", messages, max_output_tokens=100, override="blind")
    sizes = []
    for got in (seer, blind):
        image = got.calls[0].messages[0].parts[1]
        assert isinstance(image, ImagePart)
        assert image.data
        sizes.append((image.width, image.height, image.detail))
    assert sizes == [(2576, 1449, None), (1024, 576, "high")]
    assert messages[0].images[0].data is None  # the stored message never holds the bytes

    blind_only = router({"m-blind": ok()}, cfg())
    blind_only.images = store
    with pytest.raises(CantSeeImagesError):
        await blind_only.chat("hard", messages, max_output_tokens=100, override="blind")


def test_images_count_in_the_estimate_as_their_provider_bills_them(tmp_path: Path) -> None:
    config = cfg(blind="small")
    messages = with_image(ImageStore(tmp_path))
    text_only = [Message.user("what is this?")]
    for profile in ("big", "small"):
        vision = config.vision[profile]
        extra = estimate_tokens(messages, vision=vision) - estimate_tokens(text_only, vision=vision)
        assert extra == image_tokens(3840, 2160, vision)
    assert image_tokens(3840, 2160, config.vision["big"]) == 4784
