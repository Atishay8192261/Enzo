"""How big an image is sent, and what it costs (spec §5.2: images count against the budget).

Both providers Enzo knows publish the same shape of rule: the image is covered with square patches,
each one a token (OpenAI then multiplies), and an image too big for the model's edge or patch limit
is scaled down, keeping its aspect ratio, to the largest size that fits. Enzo does that scaling
itself before sending (``fit``), so the provider never resizes behind its back and the cost is
known exactly (``image_tokens``).
"""

import math

from enzo.brain.config import VisionConfig

#: Cost of an image for a brain whose vision profile is unknown: Claude's largest image (4,784
#: visual tokens), rounded up. Counting high only means compacting a little early.
UNKNOWN_IMAGE_TOKENS = 5_000


def patches(width: int, height: int, patch: int) -> int:
    return math.ceil(width / patch) * math.ceil(height / patch)


def fits(width: int, height: int, vision: VisionConfig) -> bool:
    return (
        width <= vision.max_edge
        and height <= vision.max_edge
        and patches(width, height, vision.patch) <= vision.max_patches
    )


def fit(width: int, height: int, vision: VisionConfig) -> tuple[int, int]:
    """The largest aspect-preserving size within the profile's limits; smaller images unchanged.

    The search along the long edge is Anthropic's reference implementation (vision-coordinates,
    "Resize your image before uploading", read 2026-11-26), with ``round`` as Python rounds, like
    the live API. It gives the size Claude itself would resize to (1920x1080 → 1456x819 on the
    standard tier), and works the same for OpenAI's patch budget.
    """
    if fits(width, height, vision):
        return width, height
    if height > width:
        h, w = fit(height, width, vision)
        return w, h
    aspect = width / height
    lo, hi = 1, width  # lo always fits; hi never does
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if fits(mid, max(round(mid / aspect), 1), vision):
            lo = mid
        else:
            hi = mid
    return lo, max(round(lo / aspect), 1)


def image_tokens(width: int, height: int, vision: VisionConfig | None) -> int:
    """Input tokens of one image of this size, as sent to a brain with this profile."""
    if vision is None:
        return UNKNOWN_IMAGE_TOKENS
    w, h = fit(width, height, vision)
    return math.ceil(patches(w, h, vision.patch) * vision.multiplier)
