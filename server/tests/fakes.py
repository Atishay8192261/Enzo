"""Obviously fake credentials for tests: low entropy, so secret scanners and readers see no key."""


def fake_key(tag: str) -> str:
    """A key-shaped value that is clearly not a real key, e.g. ``fake-first-000000000000``."""
    return f"fake-{tag}-" + "0" * 12
