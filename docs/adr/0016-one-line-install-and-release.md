# ADR 0016 — One-line install, one `enzo` command, released from a tag

- **Status:** Accepted (2026-11-19). Atishay: publish to PyPI as `enzo-agent`, the command stays `enzo`;
  trusted publishing, no API token; the release speaks only for Enzo.
- **Issue:** #23. Amends ADR 0005 for installed copies (the database is bundled).
- **Spec:** §2 (a product people can run), §9.2 (a runtime with a startup check), §11.2

## Decision

1. **Install:**
   - `curl -fsSL https://raw.githubusercontent.com/Atishay8192261/Enzo/main/install.sh | sh` installs
     uv (Astral's official installer) if it is missing, then runs `uv tool install --upgrade enzo-agent`.
   - Or `uv tool install enzo-agent`.
   - uv brings Python 3.13. There is no sudo and no system packages; the same command updates.
2. **One command:**
   - `enzo` opens the terminal app, `enzo web` the browser app.
   - `enzo doctor` checks the installation and gives a fix for each problem; `enzo update` upgrades.
   - `enzo serve` runs the gateway in the foreground, for an always-on machine later.
3. **Everything bundled:**
   - **Database:** Postgres 16 + pgvector from `pixeltable-pgserver` 0.7.2 (Apache-2.0, maintained,
     wheels for macOS arm64/x64 and Linux x64/arm64). It listens on a Unix socket inside
     `~/.local/share/enzo/postgres`, never on a port. It starts with the gateway, and migrations run
     on every start.
   - **Terminal app:** `bun build --compile`, one binary per platform. Every platform's OpenTUI
     native package is installed (pnpm `supportedArchitectures`), so all four cross-compile from one
     machine. macOS builds are ad-hoc signed on a macOS runner.
   - **Web app:** built into the package; the gateway serves it at `/` and accepts `/api/…` as the
     dev proxy does.
4. **Gateway lifecycle (the launcher):**
   - Opening an app starts the gateway detached if none is running. Each app registers its pid, and
     the last one to close stops the gateway and its database.
   - It only ever stops a gateway it started (that gateway writes its own pid file), never one a
     developer ran by hand.
   - A file lock keeps simultaneous opens and closes safe. Closing the terminal window (SIGHUP) or a
     kill (SIGTERM) still releases the gateway.
5. **Integrity:**
   - The release workflow writes each terminal binary's SHA-256 into the package (`_release.json`)
     before building it.
   - `enzo` downloads the binary for its own version from the GitHub release on first run, verifies
     the hash, and refuses a mismatch. So the PyPI package pins exactly which bytes run.
6. **Release (`.github/workflows/release.yml`):**
   - A tag `v<version>` matching `server/pyproject.toml` builds the terminal apps, the web app and
     the package.
   - It rehearses the install on Linux and macOS: install.sh, doctor, a verified download, and the
     launcher on the bundled database.
   - It then creates the GitHub release (binaries and install.sh) and publishes to PyPI from the
     `pypi` environment with trusted publishing (OIDC).
   - Pull requests that touch the release run everything except publishing.
7. **Hardening that came with it:**
   - The gateway answers only to local host names (DNS-rebinding defence) outside tests.
   - Enzo's data and state folders are owner-only (0700).

## Consequences

- A user needs only `curl` (or uv). First run downloads about 100 MB (the terminal app) once per
  version.
- Windows is not supported yet; the installer says to use WSL.
- **Developers are unaffected:** `scripts/dev.sh` still uses a system Postgres and Bun from source.
  The migrations moved into the package (`server/src/enzo/migrations`), and `alembic.ini` points
  there.
- **Root:** when run as root, pgserver runs Postgres as a separate system user, which then needs
  access to the install folders. Normal users never hit this; CI runners are not root.
