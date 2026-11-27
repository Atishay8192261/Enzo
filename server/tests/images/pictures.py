"""Test images made on the fly: photos with camera metadata and a GPS position, screenshots."""

import io

from PIL import Image, ImageCms, PngImagePlugin

#: A camera's EXIF: maker, model, time, and a GPS position (Paris).
MAKER = "Enzo Test Camera"


def photo(width: int = 3000, height: int = 2000, *, orientation: int | None = None) -> bytes:
    """A JPEG with EXIF (camera, GPS), XMP-free; ``orientation`` sets the EXIF rotation tag."""
    im = Image.new("RGB", (width, height), (10, 120, 200))
    for x in range(0, width, 50):  # some detail, so JPEG sizes are realistic
        im.paste((250, 250, 250), (x, 0, x + 5, height))
    exif = Image.Exif()
    exif[0x010F] = MAKER  # Make
    exif[0x0132] = "2026:11:26 10:00:00"  # DateTime
    if orientation is not None:
        exif[0x0112] = orientation
    exif[0x8825] = {1: "N", 2: (48.0, 51.0, 24.0), 3: "E", 4: (2.0, 21.0, 3.0)}  # GPSInfo
    out = io.BytesIO()
    im.save(out, "JPEG", exif=exif.tobytes(), quality=90)
    return out.getvalue()


def screenshot(width: int = 1920, height: int = 1080, *, alpha: bool = False) -> bytes:
    """A PNG with a text chunk and an ICC profile, like a macOS screenshot."""
    im = Image.new("RGBA" if alpha else "RGB", (width, height), (240, 240, 240))
    im.paste((20, 20, 20), (100, 100, 900, 140))
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "taken at the user's desk")
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    out = io.BytesIO()
    im.save(out, "PNG", pnginfo=info, icc_profile=icc)
    return out.getvalue()


def opened(data: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(data))
    im.load()
    return im
