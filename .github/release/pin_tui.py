"""Write server/src/enzo/_release.json: the version and the SHA-256 of every terminal-app binary.

The package published to PyPI then pins exactly which binaries `enzo` may download and run.
Usage: python pin_tui.py <folder with enzo-tui-*> <version>
"""

import hashlib
import json
import sys
from pathlib import Path

TARGETS = ("darwin-arm64", "darwin-x64", "linux-x64", "linux-arm64")
folder, version = Path(sys.argv[1]), sys.argv[2]
tui = {}
for target in TARGETS:
    binary = folder / f"enzo-tui-{target}"
    if not binary.is_file():
        sys.exit(f"missing {binary}: every platform must be built before the package is")
    tui[target] = {
        "name": binary.name,
        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "size": binary.stat().st_size,
    }
out = Path("server/src/enzo/_release.json")
out.write_text(json.dumps({"version": version, "tui": tui}, indent=2) + "\n", encoding="utf-8")
print(f"pinned {len(tui)} terminal-app builds for {version} in {out}")
