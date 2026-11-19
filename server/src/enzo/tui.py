"""The terminal app: one compiled binary per platform, fetched once per Enzo version (ADR 0016).

The release workflow builds the binaries, attaches them to the GitHub release ``v<version>``, and
writes their SHA-256 into this package (``_release.json``) before it is built and published. So
the package from PyPI pins exactly which bytes it will run: a download that does not match is
refused.
"""

import hashlib
import json
import os
import platform
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import httpx2

from enzo import __version__
from enzo.paths import private_dir

REPO = "Atishay8192261/Enzo"
PLATFORMS = {
    ("Darwin", "arm64"): "darwin-arm64",
    ("Darwin", "x86_64"): "darwin-x64",
    ("Linux", "x86_64"): "linux-x64",
    ("Linux", "aarch64"): "linux-arm64",
    ("Linux", "arm64"): "linux-arm64",
}


class TuiError(Exception):
    """Why the terminal app can't run. The message is shown to the user as is."""


@dataclass(frozen=True, slots=True)
class Asset:
    name: str
    sha256: str
    size: int


def current_platform() -> str:
    key = (platform.system(), platform.machine())
    target = PLATFORMS.get(key)
    if target is None:
        msg = (
            f"Enzo's terminal app doesn't run on {key[0]} {key[1]} yet. It runs on macOS "
            "(Apple silicon or Intel) and Linux (x64 or arm64); on Windows, use WSL."
        )
        raise TuiError(msg)
    return target


def release_assets() -> dict[str, Asset] | None:
    """The binaries this package was released with, or None for a copy not built as a release."""
    try:
        raw = (resources.files("enzo") / "_release.json").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    meta = json.loads(raw)
    if meta.get("version") != __version__:
        return None
    return {target: Asset(**asset) for target, asset in meta["tui"].items()}


def path(data: Path) -> Path:
    """Where this version's binary lives (``ENZO_TUI`` points at another one, e.g. a dev build)."""
    if override := os.environ.get("ENZO_TUI"):
        return Path(override).expanduser()
    return data / "bin" / f"enzo-tui-{__version__}"


def ensure(
    data: Path,
    http: httpx2.Client,
    *,
    progress: Callable[[str], None] = lambda _message: None,
) -> Path:
    """The binary for this version, downloaded and verified the first time it is needed."""
    target = path(data)
    if os.environ.get("ENZO_TUI"):
        if not target.is_file():
            msg = f"ENZO_TUI points at {target}, which does not exist."
            raise TuiError(msg)
        return target
    assets = release_assets()
    if assets is None:
        msg = (
            "This copy of Enzo was not installed from a release, so it has no terminal app to "
            "fetch. From a clone, run scripts/dev.sh instead."
        )
        raise TuiError(msg)
    asset = assets.get(current_platform())
    if asset is None:
        msg = f"This release has no terminal app for {current_platform()}."
        raise TuiError(msg)
    if target.is_file() and _sha256(target) == asset.sha256:
        return target
    # ENZO_RELEASE_BASE: a mirror (or a local rehearsal). Safe: the pinned hash must still match.
    base = os.environ.get("ENZO_RELEASE_BASE") or f"https://github.com/{REPO}/releases/download"
    url = f"{base.rstrip('/')}/v{__version__}/{asset.name}"
    progress(f"Downloading Enzo's terminal app ({asset.size // 1_000_000} MB, once per version)…")
    _download(http, url, asset, target)
    return target


def _download(http: httpx2.Client, url: str, asset: Asset, target: Path) -> None:
    private_dir(target.parent.parent)  # Enzo's data folder itself is the user's alone
    folder = private_dir(target.parent)
    digest = hashlib.sha256()
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".enzo-tui-")
    try:
        with os.fdopen(fd, "wb") as out, http.stream("GET", url, follow_redirects=True) as reply:
            if reply.status_code != httpx2.codes.OK:
                msg = f"Could not download the terminal app ({reply.status_code} from GitHub)."
                raise TuiError(msg)
            for chunk in reply.iter_bytes():
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != asset.sha256:
            msg = "The downloaded terminal app does not match this release, so it was not used."
            raise TuiError(msg)
        Path(tmp).chmod(0o700)  # runnable by the user only, like the rest of Enzo's files
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _sha256(file: Path) -> str:
    digest = hashlib.sha256()
    with file.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
