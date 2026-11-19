# Releasing Enzo

Releases are cut from a tag. Design: ADR 0016.

## One-time setup (PyPI trusted publishing)

On PyPI, add a **pending trusted publisher** for the project `enzo-agent`: Your account → Publishing →
"Add a new pending publisher" → GitHub. Use exactly these values:

| Field | Value |
| --- | --- |
| PyPI Project Name | `enzo-agent` |
| Owner | `Atishay8192261` |
| Repository name | `Enzo` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

There is no API token anywhere: PyPI trusts this workflow's GitHub identity (OIDC). On GitHub,
the `pypi` environment is created on the first release. To approve each publish by hand, add a
"Required reviewers" rule under Settings → Environments → `pypi`.

## Cutting a release

1. In a normal PR, set `version` in `server/pyproject.toml` (for example `0.2.0`) and merge it.
2. Tag that commit on `main` and push the tag:

   ```bash
   git tag v0.2.0 origin/main && git push origin v0.2.0
   ```

3. The **Release** workflow runs:
   - It checks that the tag matches the version.
   - It builds the four terminal apps (smoke-tested and signed), the web app and the package.
   - It rehearses the install on Linux and macOS.
   - It creates the GitHub release with the binaries and `install.sh`, then publishes to PyPI.

If any step fails, nothing is published after it. Fix it, delete the tag, and tag again. A version
that reached PyPI can never be uploaded again: release the next version instead.

## Checking a release

```bash
curl -fsSL https://raw.githubusercontent.com/Atishay8192261/Enzo/main/install.sh | sh
```

```bash
enzo doctor && enzo
```

## Rehearsing without publishing

Every PR that touches the release (the workflow, `install.sh`, the launcher, the terminal app) runs
the build and install rehearsal. To try a wheel by hand, point the installer at it and serve the
terminal-app builds from a folder laid out like the release (`v<version>/enzo-tui-<platform>`):

```bash
ENZO_PACKAGE=dist/python/enzo_agent-0.1.0-py3-none-any.whl sh install.sh
```

```bash
ENZO_RELEASE_BASE=http://127.0.0.1:8899 enzo
```
