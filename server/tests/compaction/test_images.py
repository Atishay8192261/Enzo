"""Images and compaction (ADR 0033): a summary replaces older images with their id and a short
description; their bytes are no longer sent, and the brain can look at one again."""

from pathlib import Path

from enzo.brain.config import BrainsConfig
from enzo.brain.models import ImagePart, Message, TextPart, ToolCall, ToolResult
from enzo.compaction.summary import fold
from enzo.images import ImageStore
from enzo.images.tools import image_tools
from enzo.tools import ToolRegistry
from tests.compaction.fakes import config
from tests.compaction.test_window import chat, main_requests, run
from tests.images.pictures import screenshot

TINY = {"max_edge": 64, "patch": 8, "max_patches": 64, "source": "test"}


def seeing() -> BrainsConfig:
    data = config().model_dump()
    data["vision"] = {"tiny": TINY}
    for name in ("main", "quick"):
        data["brains"][name]["vision"] = "tiny"
    return BrainsConfig.model_validate(data)


async def test_older_images_become_a_pointer_and_a_description(tmp_path: Path) -> None:
    kept = ImageStore(tmp_path).keep(screenshot(640, 400), source="pasted")
    history = chat(24)
    with_image = Message(
        role="user", parts=(TextPart(text="what does this error mean?"), kept.part())
    )
    history[3] = with_image  # turn 2's message carries a screenshot
    history.append(Message.user("and now?"))
    _, out, window, fakes = await run(history, cfg=seeing())

    record = window.made[0]
    assert record.turns > 2  # turn 2 was folded
    assert [r.id for r in record.summary.images] == [kept.id]
    assert record.summary.images[0].what == "an image the user sent in turn 2"
    # The summarizer read a pointer, never the image.
    summarizer = fakes["quick-1"].requests[0]
    assert f"[image {kept.id}, 640x400, the user sent]" in summarizer.text
    assert not any(m.images for m in summarizer.messages)
    # The reply's prompt holds no image any more, and says how to look again.
    sent = main_requests(fakes)[0].messages
    assert not any(m.images for m in sent)
    assert f"- {kept.id}: an image the user sent in turn 2" in sent[1].text
    assert "view_image" in sent[1].text
    assert out.messages[3] == with_image  # the transcript keeps it


def test_fold_lists_tool_images_too() -> None:
    part = ImagePart(id="img_00000000000000aa", media_type="image/png", width=10, height=20)
    turns = {
        0: [
            Message.user("look at the page"),
            Message.assistant(
                None, (ToolCall(id="c", name="browser_screenshot", arguments_json="{}"),)
            ),
            Message.tool(
                ToolResult(call_id="c", name="browser_screenshot", content="{}", images=(part,))
            ),
        ]
    }
    folded = fold(turns, None, limit=1000)
    assert folded.images == {part.id: "an image a step of Enzo's showed in turn 1"}


async def test_view_image_brings_one_back_for_a_brain_that_can_see(tmp_path: Path) -> None:
    store = ImageStore(tmp_path)
    kept = store.keep(screenshot(64, 64), source="pasted")
    registry = ToolRegistry(image_tools(store))
    call = ToolCall(id="v", name="view_image", arguments_json=f'{{"id": "{kept.id}"}}')
    outcome = await registry.execute(call)
    assert outcome.status == "ok"
    assert outcome.result.images == (kept.part(),)
    assert kept.id in outcome.result.content
    assert registry.specs(vision=False) == []  # never offered to a brain that cannot see
    missing = ToolCall(id="m", name="view_image", arguments_json='{"id": "img_0000000000000000"}')
    assert (await registry.execute(missing)).status == "failed"
