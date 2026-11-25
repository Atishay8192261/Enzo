"""Keep secrets out of what Enzo writes for people to read: eval results and traces (§11.3).

Two layers: the exact values Enzo knows are secret (the keys of the accounts it used), and the
shapes providers give keys and tokens (``sk-…``, ``xai-…``, a JSON web token, a bearer header),
including OpenAI's masked echo of a rejected key (``sk-proj-****abcd``).
"""

import re
from collections.abc import Iterable

from enzo.memory.guard import KEY_SHAPES

REDACTED = "[redacted]"
#: Shorter values are not treated as secrets: replacing them would mangle ordinary words.
MIN_SECRET = 8
_MORE = re.compile(
    r"\bsk-[A-Za-z0-9_*.-]{6,}"  # also a masked key, as a provider's error echoes it
    r"|\b[Bb]earer\s+[A-Za-z0-9._~+/=-]{8,}"
)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """``text`` with every known secret and key-shaped string replaced by ``[redacted]``."""
    for secret in sorted({s for s in secrets if len(s) >= MIN_SECRET}, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    text = KEY_SHAPES.sub(REDACTED, text)
    return _MORE.sub(REDACTED, text)
