import base64
import io
import json
import struct
import zlib
from pathlib import Path

import pytest
from PIL import Image

from enzo.brain.config import VisionConfig, load_config
from enzo.brain.models import ImagePart, Message, TextPart, ToolResult
from enzo.images import ImageError, ImageStore
from enzo.images.store import KEEP_EDGE, MAX_BYTES, MISSING
from tests.images.pictures import MAKER, opened, photo, screenshot

CONFIG = load_config()
CLAUDE = CONFIG.vision["claude"]
GPT = CONFIG.vision["gpt"]


def test_a_photo_is_kept_without_exif_or_gps_and_says_what_was_stripped(tmp_path: Path) -> None:
    raw = photo()
    assert MAKER.encode() in raw  # the camera's metadata is really in the file
    store = ImageStore(tmp_path)
    kept = store.keep(raw, source="pasted", name="/Users/me/Desktop/IMG_0001.JPG", chat="c1")
    assert (kept.media_type, kept.width, kept.height) == ("image/jpeg", 3000, 2000)
    assert kept.stripped == ("EXIF", "GPS position")
    assert kept.name == "IMG_0001.JPG"  # never the folder it came from
    data = store.read(kept)
    assert MAKER.encode() not in data
    im = opened(data)
    assert len(im.getexif()) == 0
    assert "exif" not in im.info
    meta = json.loads((tmp_path / f"{kept.id}.json").read_text())
    assert meta["source"] == "pasted"
    assert meta["chat"] == "c1"
    assert meta["created_at"]


def test_a_screenshot_stays_png_without_text_or_icc(tmp_path: Path) -> None:
    raw = screenshot()
    assert b"taken at the user's desk" in raw
    kept = ImageStore(tmp_path).keep(raw, source="file")
    assert kept.media_type == "image/png"
    assert set(kept.stripped) == {"ICC profile", "text"}
    data = (tmp_path / f"{kept.id}.png").read_bytes()
    assert b"desk" not in data
    assert b"iCCP" not in data
    assert "icc_profile" not in opened(data).info


def test_the_rotation_tag_is_applied_before_it_is_dropped(tmp_path: Path) -> None:
    kept = ImageStore(tmp_path).keep(photo(300, 200, orientation=6), source="pasted")
    assert (kept.width, kept.height) == (200, 300)  # turned upright, like the camera meant


def test_files_are_private_and_keeping_twice_keeps_once(tmp_path: Path) -> None:
    root = tmp_path / "images"
    store = ImageStore(root)
    first = store.keep(screenshot(), source="pasted")
    again = store.keep(screenshot(), source="file")
    assert again == first  # same id, first provenance
    assert root.stat().st_mode & 0o777 == 0o700
    assert {p.stat().st_mode & 0o777 for p in root.iterdir()} == {0o600}
    assert len(list(root.iterdir())) == 2


def test_transparency_is_kept(tmp_path: Path) -> None:
    kept = ImageStore(tmp_path).keep(screenshot(64, 64, alpha=True), source="pasted")
    assert opened((tmp_path / f"{kept.id}.png").read_bytes()).mode == "RGBA"


def test_a_huge_image_is_kept_at_most_keep_edge(tmp_path: Path) -> None:
    kept = ImageStore(tmp_path).keep(photo(6000, 3000), source="file")
    assert (kept.width, kept.height) == (KEEP_EDGE, KEEP_EDGE // 2)


def _png_header(width: int, height: int) -> bytes:
    """A tiny PNG that claims a huge size (a decompression bomb's header)."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\0"))


@pytest.mark.parametrize(
    ("data", "status", "words"),
    [
        (b"", 422, "empty"),
        (b"not an image at all", 415, "isn't an image"),
        (_png_header(8000, 7000), 413, "56 megapixels"),
        (b"\0" * (MAX_BYTES + 1), 413, "too big"),
    ],
)
def test_what_enzo_refuses_is_said_plainly(
    tmp_path: Path, data: bytes, status: int, words: str
) -> None:
    with pytest.raises(ImageError, match=words) as refused:
        ImageStore(tmp_path).keep(data, source="pasted")
    assert refused.value.status == status
    assert not list(tmp_path.iterdir())


def test_each_brain_gets_its_own_size_once(tmp_path: Path) -> None:
    store = ImageStore(tmp_path)
    kept = store.keep(screenshot(3840, 2160), source="pasted")
    claude = store.wire(kept.id, CLAUDE)
    gpt = store.wire(kept.id, GPT)
    assert claude is not None
    assert gpt is not None
    assert (claude.width, claude.height) == (2576, 1449)
    assert (gpt.width, gpt.height) == (2048, 1152)
    assert opened(base64.b64decode(gpt.data)).size == (2048, 1152)
    copies = sorted(p.name for p in tmp_path.iterdir() if "-" in p.name)
    assert copies == [f"{kept.id}-2048x1152.png", f"{kept.id}-2576x1449-10000000.png"]
    fresh = ImageStore(tmp_path).wire(kept.id, GPT)  # a restarted gateway reads the same bytes
    assert fresh == gpt


def test_a_brain_with_a_small_byte_limit_gets_a_smaller_jpeg(tmp_path: Path) -> None:
    store = ImageStore(tmp_path)
    noisy = Image.effect_noise((1500, 1500), 100).convert("RGB")
    out = io.BytesIO()
    noisy.save(out, "PNG")
    kept = store.keep(out.getvalue(), source="pasted")
    small = VisionConfig(max_edge=1500, patch=28, max_patches=99_999, max_bytes=300_000, source="t")
    copy = store.wire(kept.id, small)
    assert copy is not None
    assert copy.media_type == "image/jpeg"
    assert len(copy.data) <= 300_000


async def test_attach_fills_in_each_image_and_says_when_one_is_gone(tmp_path: Path) -> None:
    store = ImageStore(tmp_path)
    kept = store.keep(screenshot(), source="pasted")
    gone = ImagePart(id="img_0123456789abcdef", media_type="image/png", width=5, height=5)
    messages = [
        Message(role="user", parts=(TextPart(text="what is this?"), kept.part(), gone)),
        Message.tool(
            ToolResult(call_id="c", name="shot", content="{}", images=(kept.part(), gone))
        ),
    ]
    sent = await store.attach(messages, CLAUDE)
    user, tool = sent
    first = user.parts[1]
    assert isinstance(first, ImagePart)
    assert first.data is not None
    assert user.parts[2] == TextPart(text=MISSING)
    result = tool.parts[0]
    assert isinstance(result, ToolResult)
    assert [i.data is not None for i in result.images] == [True]
    assert result.content.endswith(MISSING)
    # Bytes are never stored or shown: they leave every dump and repr.
    assert "data" not in json.dumps(user.model_dump(mode="json"))
    assert first.data not in repr(first)
