#!/bin/sh
# Install Enzo, your private personal AI agent:
#
#   curl -fsSL https://raw.githubusercontent.com/Atishay8192261/Enzo/main/install.sh | sh
#
# What it does, all inside your home folder and without sudo:
#   1. installs uv (Astral's Python tool manager) if you don't have it; uv brings Python itself
#   2. installs Enzo with `uv tool install enzo-agent` (running this again updates it)
# The database and the terminal app come with Enzo: the terminal app is downloaded and checked
# the first time you run `enzo`.
#
# ENZO_PACKAGE installs something else instead (a wheel file, or e.g. enzo-agent==0.1.0).
set -eu

say() { printf '%s\n' "$*"; }
fail() {
  printf 'Enzo could not be installed: %s\n' "$*" >&2
  exit 1
}

check_platform() {
  os=$(uname -s)
  arch=$(uname -m)
  case "$os" in
    Darwin | Linux) ;;
    MINGW* | MSYS* | CYGWIN*) fail "Windows isn't supported yet. Install WSL, then run this inside it." ;;
    *) fail "$os isn't supported yet (macOS and Linux are)." ;;
  esac
  case "$arch" in
    x86_64 | amd64 | arm64 | aarch64) ;;
    *) fail "$arch processors aren't supported yet (x64 and arm64 are)." ;;
  esac
}

find_uv() {
  for candidate in "$(command -v uv 2>/dev/null || true)" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      UV=$candidate
      return 0
    fi
  done
  return 1
}

ensure_uv() {
  if find_uv; then
    say "Using uv at $UV"
    return
  fi
  command -v curl >/dev/null 2>&1 || fail "curl is needed to download uv."
  say "Installing uv (https://docs.astral.sh/uv/)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh || fail "the uv installer failed (see above)."
  find_uv || fail "uv was installed but can't be found. Open a new terminal and run this again."
}

main() {
  say "Installing Enzo…"
  check_platform
  ensure_uv
  package=${ENZO_PACKAGE:-enzo-agent}
  "$UV" tool install --upgrade --python 3.13 "$package" || fail "uv could not install $package."
  bin=$("$UV" tool dir --bin)
  say ""
  if command -v enzo >/dev/null 2>&1; then
    say "Enzo is installed. Start it with:  enzo"
  else
    say "Enzo is installed in $bin."
    say "Open a new terminal (or run: export PATH=\"$bin:\$PATH\"), then start it with:  enzo"
  fi
  say "Check everything with:  enzo doctor"
}

main "$@"
