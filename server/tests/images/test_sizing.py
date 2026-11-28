import pytest

from enzo.brain.config import VisionConfig, load_config
from enzo.images.sizing import UNKNOWN_IMAGE_TOKENS, fit, image_tokens

CONFIG = load_config()
CLAUDE = CONFIG.vision["claude"]
GPT = CONFIG.vision["gpt"]
#: Claude's standard tier, for the published examples.
STANDARD = VisionConfig(max_edge=1568, patch=28, max_patches=1568, source="t")


@pytest.mark.parametrize(
    ("size", "standard", "high"),
    [
        # Anthropic's own table (vision, "Resolution and token cost", read 2026-11-26).
        ((200, 200), ((200, 200), 64), ((200, 200), 64)),
        ((1000, 1000), ((1000, 1000), 1296), ((1000, 1000), 1296)),
        ((1092, 1092), ((1092, 1092), 1521), ((1092, 1092), 1521)),
        ((1920, 1080), ((1456, 819), 1560), ((1920, 1080), 2691)),
        # The table says 1269x952; Anthropic's reference implementation gives 1270x952 (same cost).
        ((2000, 1500), ((1270, 952), 1564), ((2000, 1500), 3888)),
        ((3840, 2160), ((1456, 819), 1560), ((2576, 1449), 4784)),
        ((1075, 1520), ((924, 1307), None), ((1075, 1520), None)),  # the A4 scan, portrait
    ],
)
def test_claude_sizes_and_costs_match_anthropics_published_examples(
    size: tuple[int, int],
    standard: tuple[tuple[int, int], int | None],
    high: tuple[tuple[int, int], int | None],
) -> None:
    for vision, (want, tokens) in ((STANDARD, standard), (CLAUDE, high)):
        assert fit(*size, vision) == want
        if tokens is not None:
            assert image_tokens(*size, vision) == tokens


def test_openai_patch_budget_matches_the_published_examples() -> None:
    # images-vision guide, gpt-6-astra "high": 1024x1024 → 1229; 2048x2048 → 1600x1600, 3000.
    assert image_tokens(1024, 1024, GPT) == 1229
    assert fit(2048, 2048, GPT) == (1600, 1600)
    assert image_tokens(2048, 2048, GPT) == 3000


def test_an_unknown_profile_counts_high() -> None:
    assert image_tokens(10, 10, None) == UNKNOWN_IMAGE_TOKENS >= image_tokens(9000, 9000, CLAUDE)
