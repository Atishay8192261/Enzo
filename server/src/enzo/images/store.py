"""Images the user shows Enzo, and the ones its tools take, kept with provenance (spec §11.2).

- **Private:** one folder in Enzo's data folder (``~/.local/share/enzo/images``, 0700), files
  readable by the user only (0600). Bytes are never logged; messages refer to an image by its id.
- **Clean before anything else:** an image is decoded, turned upright, and written again from its
  pixels alone, so EXIF (camera, time, GPS position), XMP, ICC and text chunks are gone before it
  is stored, let alone sent (§12.5). Only PNG and JPEG are kept: every provider takes both.
- **Limits:** at most ``MAX_BYTES`` per file and ``MAX_PIXELS`` pixels (a decompression bomb is
  refused before it is decoded); the kept copy is at most ``KEEP_EDGE`` pixels on its long side.
- **Provenance:** beside each image, when it came, from what input (pasted, a file, a screenshot, a
  tool), which tool and brain made it, in which chat, and what was stripped.
- **Each brain gets its own size:** ``wire`` scales the kept copy to the brain's vision profile
  (``enzo.images.sizing``) and keeps that copy too, so the bytes are the same on every call
  (prompt caches stay warm) and the work is done once.
- Ids are a hash of the cleaned image: keeping the same image twice keeps it once.
"""

import asyncio
import base64
import contextlib
import hashlib
import io
import json
import logging
import os
import re
import tempfile
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from PIL import Image, ImageOps, UnidentifiedImageError

from enzo.brain.config import VisionConfig
from enzo.brain.models import IMAGE_ID_PATTERN, ImagePart, Message, Part, TextPart, ToolResult
from enzo.images.sizing import fit
from enzo.paths import private_dir

log = logging.getLogger("enzo.images")

type Source = Literal["pasted", "file", "screenshot", "tool"]
type MediaType = Literal["image/png", "image/jpeg"]

#: The largest image file Enzo takes (a 4K screenshot as PNG is well under it).
MAX_BYTES = 20 * 1024 * 1024
#: The most pixels an image may have (a 48-megapixel phone photo fits).
MAX_PIXELS = 50_000_000
#: The kept copy's longest side: enough for every model Enzo knows (Claude's is 2,576).
KEEP_EDGE = 4096
#: What Enzo reads. HEIC is not among them: Pillow cannot read it without a plugin.
READS = {"PNG": "PNG", "JPEG": "JPEG", "MPO": "JPEG", "GIF": "GIF", "WEBP": "WebP", "BMP": "BMP"}
READS_WORDS = "PNG, JPEG, GIF, WebP or BMP"
JPEG_QUALITY = 90
#: Lower qualities tried, in order, when a brain's per-image size limit is smaller.
SMALLER_QUALITIES = (80, 65, 50)
#: Copies made for brains kept in memory, so every call of a turn does not read them again.
MEMORY_COPIES = 16
MISSING = "[An image was here, but Enzo no longer has its file, so it cannot be shown.]"


class ImageError(Exception):
    """An image Enzo cannot take, in plain words. ``status``: the HTTP status for it."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class StoredImage:
    """An image Enzo keeps, and where it came from."""

    id: str
    media_type: MediaType
    width: int
    height: int
    bytes: int
    created_at: datetime
    source: Source
    #: The file's name as the user had it, if any (shown to them, never sent to a brain).
    name: str | None = None
    #: The tool that made it (a screenshot), and the brain whose step asked for it.
    tool: str | None = None
    brain: str | None = None
    #: The chat it was first kept for, when known.
    chat: str | None = None
    #: What was removed before it was kept: "EXIF", "GPS position", "XMP", "ICC profile", ...
    stripped: tuple[str, ...] = ()

    def part(self) -> ImagePart:
        return ImagePart(
            id=self.id, media_type=self.media_type, width=self.width, height=self.height
        )

    @property
    def words(self) -> str:
        """Where it came from, in plain words: "an image the user pasted"."""
        if self.source == "screenshot":
            return "a screenshot Enzo took"
        if self.source == "tool":
            return "an image from a step Enzo took"
        return "an image the user attached" if self.source == "file" else "an image the user pasted"


@dataclass(frozen=True, slots=True)
class Wire:
    """One brain's copy of an image: what is sent."""

    media_type: MediaType
    data: str  # base64
    width: int
    height: int


def image_id(data: bytes) -> str:
    return "img_" + hashlib.sha256(data).hexdigest()[:16]


class ImageStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._copies: OrderedDict[tuple[str, int, int, int | None], Wire] = OrderedDict()

    # -- keeping ------------------------------------------------------------------------------

    def keep(
        self,
        data: bytes,
        *,
        source: Source,
        name: str | None = None,
        tool: str | None = None,
        brain: str | None = None,
        chat: str | None = None,
    ) -> StoredImage:
        """Clean ``data`` and keep it (once: the same image keeps its first provenance)."""
        cleaned, media_type, width, height, stripped = clean(data)
        found = image_id(cleaned)
        existing = self.get(found)
        if existing is not None:
            return existing
        stored = StoredImage(
            id=found,
            media_type=media_type,
            width=width,
            height=height,
            bytes=len(cleaned),
            created_at=datetime.now(UTC),
            source=source,
            name=_plain_name(name),
            tool=tool,
            brain=brain,
            chat=chat,
            stripped=stripped,
        )
        folder = private_dir(self.root)
        _write(folder / f"{found}{_ext(media_type)}", cleaned)
        _write(folder / f"{found}.json", json.dumps(_provenance(stored)).encode())
        log.info(
            "image.kept",
            extra={"image": found, "source": source, "width": width, "height": height},
        )
        return stored

    async def keep_async(
        self,
        data: bytes,
        *,
        source: Source,
        name: str | None = None,
        tool: str | None = None,
        brain: str | None = None,
        chat: str | None = None,
    ) -> StoredImage:
        """``keep`` in a worker thread: decoding a big photo must not stall the gateway."""
        return await asyncio.to_thread(
            self.keep, data, source=source, name=name, tool=tool, brain=brain, chat=chat
        )

    def get(self, found: str) -> StoredImage | None:
        if not re.fullmatch(IMAGE_ID_PATTERN, found):
            return None
        try:
            meta = json.loads((self.root / f"{found}.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None
        return StoredImage(
            id=found,
            media_type=meta["media_type"],
            width=int(meta["width"]),
            height=int(meta["height"]),
            bytes=int(meta["bytes"]),
            created_at=datetime.fromisoformat(meta["created_at"]),
            source=meta["source"],
            name=meta.get("name"),
            tool=meta.get("tool"),
            brain=meta.get("brain"),
            chat=meta.get("chat"),
            stripped=tuple(meta.get("stripped", ())),
        )

    def read(self, stored: StoredImage) -> bytes:
        return (self.root / f"{stored.id}{_ext(stored.media_type)}").read_bytes()

    # -- sending ------------------------------------------------------------------------------

    def wire(self, found: str, vision: VisionConfig) -> Wire | None:
        """This image as a brain with ``vision`` gets it; None when its file is gone."""
        stored = self.get(found)
        if stored is None:
            return None
        width, height = fit(stored.width, stored.height, vision)
        key = (found, width, height, vision.max_bytes)
        if (copy := self._copies.get(key)) is not None:
            self._copies.move_to_end(key)
            return copy
        try:
            copy = self._copy(stored, width, height, vision.max_bytes)
        except FileNotFoundError:
            return None
        self._copies[key] = copy
        while len(self._copies) > MEMORY_COPIES:
            self._copies.popitem(last=False)
        return copy

    async def attach(self, messages: Sequence[Message], vision: VisionConfig) -> list[Message]:
        """The messages with each image's bytes filled in for this brain (for one call only).

        An image whose file is gone is replaced by a note saying so: never dropped silently.
        """
        if not any(m.images for m in messages):
            return list(messages)
        return await asyncio.to_thread(self._attach, messages, vision)

    def _attach(self, messages: Sequence[Message], vision: VisionConfig) -> list[Message]:
        out: list[Message] = []
        for message in messages:
            if not message.images:
                out.append(message)
                continue
            parts: list[Part] = []
            for part in message.parts:
                if isinstance(part, ImagePart):
                    filled = self._filled(part, vision)
                    parts.append(filled if filled is not None else TextPart(text=MISSING))
                elif isinstance(part, ToolResult) and part.images:
                    shown = [self._filled(i, vision) for i in part.images]
                    kept = tuple(i for i in shown if i is not None)
                    note = "" if len(kept) == len(shown) else f"\n{MISSING}"
                    parts.append(
                        part.model_copy(update={"images": kept, "content": part.content + note})
                    )
                else:
                    parts.append(part)
            out.append(Message(role=message.role, parts=tuple(parts)))
        return out

    def _filled(self, part: ImagePart, vision: VisionConfig) -> ImagePart | None:
        copy = self.wire(part.id, vision)
        if copy is None:
            log.warning("image.missing", extra={"image": part.id})
            return None
        return ImagePart(
            id=part.id,
            media_type=copy.media_type,
            width=copy.width,
            height=copy.height,
            data=copy.data,
            detail=vision.detail,
        )

    def _copy(self, stored: StoredImage, width: int, height: int, limit: int | None) -> Wire:
        folder = private_dir(self.root)
        suffix = f"-{limit}" if limit else ""
        for ext, media_type in ((".png", "image/png"), (".jpg", "image/jpeg")):
            cached = folder / f"{stored.id}-{width}x{height}{suffix}{ext}"
            if cached.exists():
                data = cached.read_bytes()
                with Image.open(io.BytesIO(data)) as im:
                    size = im.size
                return Wire(media_type, _b64(data), size[0], size[1])  # type: ignore[arg-type]
        original = self.read(stored)
        if (width, height) == (stored.width, stored.height) and _fits_bytes(original, limit):
            return Wire(stored.media_type, _b64(original), width, height)
        data, media_type, size = _scaled(original, stored.media_type, (width, height), limit)
        _write(folder / f"{stored.id}-{width}x{height}{suffix}{_ext(media_type)}", data)
        return Wire(media_type, _b64(data), size[0], size[1])


# -- cleaning ------------------------------------------------------------------------------------


def clean(data: bytes) -> tuple[bytes, MediaType, int, int, tuple[str, ...]]:
    """The image rewritten from its pixels alone: upright, first frame, no metadata.

    Photos (JPEG) stay JPEG; everything else (screenshots, drawings) becomes PNG, keeping
    transparency. Returns the bytes, their type, the size and what was stripped.
    """
    if not data:
        msg = "That image is empty."
        raise ImageError(msg)
    if len(data) > MAX_BYTES:
        msg = f"That image is too big ({_mb(len(data))}): the most is {_mb(MAX_BYTES)}."
        raise ImageError(msg, 413)
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.format not in READS:
                msg = f"Enzo can't read {im.format} images: use {READS_WORDS}."
                raise ImageError(msg, 415)
            if im.width * im.height > MAX_PIXELS:
                megapixels = im.width * im.height / 1e6
                msg = (
                    f"That image is too big ({im.width}x{im.height}, {megapixels:.0f} "
                    f"megapixels): the most is {MAX_PIXELS // 1_000_000} megapixels."
                )
                raise ImageError(msg, 413)
            stripped = _metadata(im)
            photo = im.format in ("JPEG", "MPO")
            im.seek(0)
            upright = ImageOps.exif_transpose(im)
            pixels = _plain_pixels(upright, photo=photo)
    except ImageError:
        raise
    except Image.DecompressionBombError:  # far over the limit: Pillow refuses to open it at all
        msg = f"That image is too big: the most is {MAX_PIXELS // 1_000_000} megapixels."
        raise ImageError(msg, 413) from None
    except UnidentifiedImageError:
        msg = f"That file isn't an image Enzo can read: use {READS_WORDS}."
        raise ImageError(msg, 415) from None
    except (OSError, ValueError, SyntaxError) as exc:
        msg = f"That image could not be read ({type(exc).__name__}); it may be damaged."
        raise ImageError(msg) from None
    if max(pixels.size) > KEEP_EDGE:
        pixels.thumbnail((KEEP_EDGE, KEEP_EDGE), Image.Resampling.LANCZOS)
    media_type: MediaType = "image/jpeg" if photo else "image/png"
    return _encode(pixels, media_type), media_type, pixels.width, pixels.height, stripped


def _metadata(im: Image.Image) -> tuple[str, ...]:
    """What the file carries besides its pixels (all of it is dropped)."""
    found: list[str] = []
    exif = im.getexif()
    if len(exif):
        found.append("EXIF")
        if exif.get_ifd(0x8825):  # GPSInfo
            found.append("GPS position")
    info = im.info
    if "xmp" in info or "XML:com.adobe.xmp" in info:
        found.append("XMP")
    if "icc_profile" in info:
        found.append("ICC profile")
    texts = {
        k
        for k in info
        if k not in ("xmp", "XML:com.adobe.xmp", "icc_profile", "exif", "dpi", "gamma")
        and isinstance(info[k], str)
    }
    if texts:
        found.append("text")
    return tuple(found)


def _plain_pixels(im: Image.Image, *, photo: bool) -> Image.Image:
    """A new image made from the pixels only: nothing else of the file can come along."""
    alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
    mode = "RGB" if photo or not alpha else "RGBA"
    converted = im.convert(mode)
    return Image.frombytes(mode, converted.size, converted.tobytes())


def _encode(im: Image.Image, media_type: MediaType, quality: int = JPEG_QUALITY) -> bytes:
    buffer = io.BytesIO()
    if media_type == "image/jpeg":
        rgb = im if im.mode == "RGB" else _flatten(im)
        rgb.save(buffer, "JPEG", quality=quality)
    else:
        im.save(buffer, "PNG")
    return buffer.getvalue()


def _flatten(im: Image.Image) -> Image.Image:
    """Transparent parts on white, for JPEG."""
    if im.mode != "RGBA":
        return im.convert("RGB")
    back = Image.new("RGB", im.size, (255, 255, 255))
    back.paste(im, mask=im.getchannel("A"))
    return back


def _scaled(
    data: bytes, media_type: MediaType, size: tuple[int, int], limit: int | None
) -> tuple[bytes, MediaType, tuple[int, int]]:
    """The kept image at ``size``; if it is still over ``limit``, as JPEG at a lower quality,
    then smaller, until it fits."""
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        work = im if im.size == size else im.resize(size, Image.Resampling.LANCZOS)
    out = _encode(work, media_type)
    if _fits_bytes(out, limit):
        return out, media_type, work.size
    while True:
        for quality in SMALLER_QUALITIES:
            out = _encode(work, "image/jpeg", quality)
            if _fits_bytes(out, limit):
                return out, "image/jpeg", work.size
        smaller = (max(1, work.width * 4 // 5), max(1, work.height * 4 // 5))
        if smaller == work.size:
            return out, "image/jpeg", work.size
        work = work.resize(smaller, Image.Resampling.LANCZOS)


def _fits_bytes(data: bytes, limit: int | None) -> bool:
    """Providers count the base64 text: 4 characters for every 3 bytes."""
    return limit is None or 4 * ((len(data) + 2) // 3) <= limit


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _ext(media_type: str) -> str:
    return ".jpg" if media_type == "image/jpeg" else ".png"


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


def _plain_name(name: str | None) -> str | None:
    """A file name as one printable line, without its folder (never a path from elsewhere)."""
    if not name:
        return None
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    words = "".join(c for c in base if c.isprintable()).strip()
    return words[:120] or None


def _provenance(stored: StoredImage) -> dict[str, object]:
    return {
        "id": stored.id,
        "media_type": stored.media_type,
        "width": stored.width,
        "height": stored.height,
        "bytes": stored.bytes,
        "created_at": stored.created_at.isoformat(),
        "source": stored.source,
        "name": stored.name,
        "tool": stored.tool,
        "brain": stored.brain,
        "chat": stored.chat,
        "stripped": list(stored.stripped),
    }


def _write(path: Path, data: bytes) -> None:
    """Written fully or not at all; mkstemp files are readable by the user only."""
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".image-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
        raise
