"""Memory never keeps secrets (spec §6.2): passwords, API keys, one-time codes, card numbers,
government IDs. A fact that looks like one is refused before it is written anywhere.

The check errs on the side of refusing: a memory that is wrongly refused can be reworded ("uses a
password manager"); a secret that is wrongly kept would sit in the database and Memory.md.
"""

import re

_SECRET_WORDS = re.compile(
    r"\b(password|passcode|passphrase|pin|otp|one[- ]time (?:code|password)|verification code|"
    r"security code|2fa code|cvv|cvc|api key|secret key|access token|recovery code)s?\b"
    r"\s*(?:is|was|:|=|-)\s*\S+",
    re.IGNORECASE,
)
# Shapes of keys and tokens providers issue, by their published prefixes.
_KEY_SHAPES = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|sk_(?:live|test)_[A-Za-z0-9]{16,}|xai-[A-Za-z0-9]{16,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|"
    r"AIza[0-9A-Za-z_-]{30,}|xox[abprs]-[A-Za-z0-9-]{10,}|tvly-[A-Za-z0-9-]{16,})"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"  # a JSON web token
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)
# A long run of letters and digits mixed, like a generated key, with no spaces.
_TOKEN = re.compile(r"(?<![\w/.-])(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{32,}")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_DIGITS = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def secret_reason(text: str) -> str | None:
    """Why ``text`` must not be kept, or None when it is fine."""
    if _KEY_SHAPES.search(text) or _TOKEN.search(text):
        return "it looks like a key or token"
    if _SECRET_WORDS.search(text):
        return "it looks like a password or code"
    if _SSN.search(text):
        return "it looks like a government ID number"
    for match in _DIGITS.finditer(text):
        if _luhn(re.sub(r"\D", "", match.group())):
            return "it looks like a card number"
    return None


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2  # the Luhn rule: double, then add the digits
        total += n
    return total % 10 == 0
