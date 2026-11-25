#!/usr/bin/env bash
# One command to run Enzo from a clone (issue #22).
#
#   scripts/dev.sh            check tools, set up, start the gateway, open the terminal app
#   scripts/dev.sh web        the same, but open the web app
#   scripts/dev.sh doctor     only check tools and the database; change nothing
#   scripts/dev.sh setup      check, install dependencies, create the database, run migrations
#
# It never runs sudo and never deletes anything. When something is missing it says exactly what
# and how to fix it, then stops. The gateway it starts is stopped when you quit the app.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/.dev"
GATEWAY_LOG="$LOG_DIR/gateway.log"
PORT="${ENZO_PORT:-8787}"
DEFAULT_DB_URL="postgresql+asyncpg://enzo:enzo@localhost:5432/enzo"
DB_URL="${ENZO_DATABASE_URL:-$DEFAULT_DB_URL}"
PSQL_URL="${DB_URL/+asyncpg/}" # psql speaks postgresql://, not the SQLAlchemy driver form
OS="$(uname -s)"
GATEWAY_PID=""
PROBLEMS=0

say() { printf '%s\n' "$*"; }
ok() { printf '  ok    %s\n' "$*"; }
problem() {
  printf '  FIX   %s\n' "$1"
  shift
  for line in "$@"; do printf '        %s\n' "$line"; done
  PROBLEMS=$((PROBLEMS + 1))
}
die() {
  printf 'enzo: %s\n' "$*" >&2
  exit 1
}

# True when version $1 >= version $2 (dotted numbers, an optional leading "v"). Pure bash:
# macOS ships bash 3.2 and a BSD sort, so no `sort -V`.
version_ge() {
  local IFS=. i h w
  local -a have want
  read -r -a have <<<"${1#v}"
  read -r -a want <<<"${2#v}"
  for i in 0 1 2; do
    h="${have[i]:-0}" w="${want[i]:-0}"
    h="${h%%[!0-9]*}" w="${w%%[!0-9]*}"
    if ((10#${h:-0} > 10#${w:-0})); then return 0; fi
    if ((10#${h:-0} < 10#${w:-0})); then return 1; fi
  done
  return 0
}

on_mac() { [ "$OS" = "Darwin" ]; }

# nodejs.org's LTS moves on every year: name the floor, not a version that goes stale.
node_fix() {
  if on_mac; then
    printf 'Install the current Node LTS (%s or newer) from https://nodejs.org: the macOS Installer (.pkg)' "$1"
  else
    printf 'Install the current Node LTS (%s or newer) from https://nodejs.org' "$1"
  fi
}

check_tools() {
  say "Checking tools"
  local want_node want_bun want_pnpm
  want_node="$(sed -n 's/.*"node": *">=\([0-9.]*\)".*/\1/p' "$ROOT/package.json")"
  want_pnpm="$(sed -n 's/.*"packageManager": *"pnpm@\([0-9.]*\)".*/\1/p' "$ROOT/package.json")"
  want_bun="$(tr -d '[:space:]' <"$ROOT/.bun-version")"

  local node_ok=0
  if ! command -v node >/dev/null; then
    problem "Node $want_node or newer is missing" "$(node_fix "$want_node")"
  elif ! version_ge "$(node -v)" "$want_node"; then
    problem "Node $(node -v) is too old: Enzo needs $want_node or newer" "$(node_fix "$want_node")"
  else
    ok "node $(node -v)"
    node_ok=1
  fi

  if [ "$node_ok" = 0 ]; then
    say "  ..    pnpm: checked once Node is fixed (it runs on Node)"
  elif ! command -v pnpm >/dev/null; then
    problem "pnpm is missing" "Run: corepack enable   (Node ships corepack; it uses pnpm $want_pnpm)"
  elif [ "$(cd "$ROOT" && pnpm --version 2>/dev/null)" != "$want_pnpm" ]; then
    problem "pnpm $(cd "$ROOT" && pnpm --version 2>/dev/null) is not the pinned $want_pnpm" \
      "Run: corepack enable   (then pnpm follows package.json's packageManager)"
  else
    ok "pnpm $want_pnpm"
  fi

  if ! command -v bun >/dev/null; then
    problem "Bun $want_bun is missing (the terminal app runs on it)" \
      "Install exactly Bun $want_bun: https://bun.sh/docs/installation"
  elif [ "$(bun --version)" != "$want_bun" ]; then
    problem "Bun $(bun --version) is not the pinned $want_bun (OpenTUI is pre-1.0)" \
      "Install exactly Bun $want_bun: https://bun.sh/docs/installation"
  else
    ok "bun $want_bun"
  fi

  if ! command -v uv >/dev/null; then
    problem "uv is missing (it also installs Python 3.13)" \
      "Install: https://docs.astral.sh/uv/getting-started/installation/"
  else
    ok "$(uv --version)"
  fi

  if ! command -v psql >/dev/null || ! command -v pg_isready >/dev/null; then
    if on_mac; then
      problem "PostgreSQL 16 is missing" "Run: brew install postgresql@16 && brew services start postgresql@16" \
        "If psql is still not found: export PATH=\"\$(brew --prefix postgresql@16)/bin:\$PATH\""
    else
      problem "PostgreSQL 16 is missing" "Run: sudo apt-get install -y postgresql-16 && sudo pg_ctlcluster 16 main start"
    fi
  else
    # Only the client: the server can be another version, and it is checked with the database.
    ok "psql client $(psql --version | sed -n 's/^psql (PostgreSQL) \([0-9.]*\).*/\1/p')"
  fi
}

check_database() {
  say "Checking the database ($(printf '%s' "$PSQL_URL" | sed 's#//[^@]*@#//#'))"
  command -v pg_isready >/dev/null || return 0 # already reported by check_tools
  if ! pg_isready -q -d "$PSQL_URL"; then
    if on_mac; then
      problem "PostgreSQL is not running" "Run: brew services start postgresql@16"
    else
      problem "PostgreSQL is not running" "Run: sudo pg_ctlcluster 16 main start"
    fi
    return 0
  fi
  check_server_version || return 0
  if psql -q -d "$PSQL_URL" -c 'SELECT 1' >/dev/null 2>&1; then
    ok "database reachable"
    return 0
  fi
  if [ "$DB_URL" != "$DEFAULT_DB_URL" ]; then
    problem "Cannot connect with ENZO_DATABASE_URL" \
      "Check the URL, or create the role and database it names (this script only creates the default)"
    return 0
  fi
  if [ "$1" = "create" ] && create_database; then
    ok "created role and database enzo"
    return 0
  fi
  database_fix
}

# Any connection that can ask the server something: the enzo database, the enzo role on the
# built-in postgres database (the enzo database may not exist yet), or the login user (Homebrew).
pg_ask() {
  local target
  for target in "$PSQL_URL" "${PSQL_URL%/*}/postgres" postgres; do
    if psql -qtA -d "$target" -c "$1" 2>/dev/null; then return 0; fi
  done
  return 1
}

# The server's version, not the psql program's (#33): Homebrew can link an older client.
check_server_version() {
  local num shown
  num="$(pg_ask 'SHOW server_version_num')" || return 0 # cannot connect yet: said below
  shown="$(pg_ask 'SHOW server_version' | sed 's/ .*//')"
  if [ "${num:-0}" -ge 160000 ] 2>/dev/null; then
    ok "PostgreSQL server $shown"
    return 0
  fi
  if on_mac; then
    problem "PostgreSQL server $shown is too old: Enzo needs 16 or newer" \
      "Run: brew services stop postgresql@${shown%%.*}" \
      "Then: brew install postgresql@16 && brew services start postgresql@16"
  else
    problem "PostgreSQL server $shown is too old: Enzo needs 16 or newer" \
      "Run: sudo apt-get install -y postgresql-16" \
      "Then stop version ${shown%%.*} and run 16 on port 5432: sudo pg_ctlcluster ${shown%%.*} main stop"
  fi
  return 1
}

# Only what is missing (#32): following a FIX must never fail on a step already done.
database_fix() {
  local sudo="" role="unknown"
  on_mac || sudo="sudo -u postgres "
  if psql -q -d "${PSQL_URL%/*}/postgres" -c 'SELECT 1' >/dev/null 2>&1; then
    role="yes" # the role signs in: only the database is missing
  elif on_mac && psql -qtA -d postgres -c 'SELECT 1' >/dev/null 2>&1; then
    if psql -qtA -d postgres -c "SELECT 1 FROM pg_roles WHERE rolname='enzo'" | grep -q 1; then
      role="no-password"
    else
      role="no"
    fi
  fi
  case "$role" in
    yes)
      problem "The enzo database is missing (the enzo role is there)" "Run: ${sudo}createdb -O enzo enzo"
      ;;
    no-password)
      problem "The enzo role cannot sign in with its password" \
        "Run: psql -d postgres -c \"ALTER ROLE enzo LOGIN PASSWORD 'enzo'\"" \
        "Then run this again (it creates the database if that is missing too)"
      ;;
    no)
      problem "The enzo role and database are missing" \
        "Run: psql -d postgres -c \"CREATE ROLE enzo LOGIN SUPERUSER PASSWORD 'enzo'\"" \
        "Then: createdb -O enzo enzo"
      ;;
    *) # only the postgres user can look on Linux: steps that work in either case
      problem "The enzo role or database is missing" \
        "Run: ${sudo}psql -c \"CREATE ROLE enzo LOGIN SUPERUSER PASSWORD 'enzo'\"   (if it says the role already exists, go on)" \
        "Then: ${sudo}createdb -O enzo enzo"
      ;;
  esac
}

# Homebrew's Postgres makes the login user a superuser, so this works there without sudo.
# Elsewhere it fails quietly and the exact commands are printed instead.
create_database() {
  psql -q -d postgres -c "SELECT 1" >/dev/null 2>&1 || return 1
  if ! psql -qtA -d postgres -c "SELECT 1 FROM pg_roles WHERE rolname='enzo'" | grep -q 1; then
    psql -q -d postgres -c "CREATE ROLE enzo LOGIN SUPERUSER PASSWORD 'enzo'" >/dev/null || return 1
  fi
  if ! psql -qtA -d postgres -c "SELECT 1 FROM pg_database WHERE datname='enzo'" | grep -q 1; then
    createdb -O enzo enzo || return 1
  fi
  psql -q -d "$PSQL_URL" -c 'SELECT 1' >/dev/null 2>&1
}

stop_if_problems() {
  if [ "$PROBLEMS" -gt 0 ]; then
    say ""
    die "$PROBLEMS thing(s) to fix first (see FIX above), then run this again."
  fi
}

install_and_migrate() {
  say "Installing dependencies and migrating"
  (cd "$ROOT" && pnpm install --frozen-lockfile --silent) || die "pnpm install failed (output above)"
  ok "JavaScript dependencies"
  (cd "$ROOT/server" && uv sync --locked --quiet) || die "uv sync failed (output above)"
  ok "Python dependencies"
  local out
  if ! out="$(cd "$ROOT/server" && ENZO_DATABASE_URL="$DB_URL" uv run --quiet alembic upgrade head 2>&1)"; then
    printf '%s\n' "$out" >&2
    die "database migration failed (output above)"
  fi
  ok "database migrated"
}

gateway_healthy() {
  curl -fsS -m 2 "http://127.0.0.1:$PORT/healthz" 2>/dev/null | grep -q '"status":"ok"'
}

start_gateway() {
  if gateway_healthy; then
    say "Using the Enzo gateway already running on port $PORT (it keeps running when you quit)"
    return 0
  fi
  if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
    die "port $PORT is in use by something that is not Enzo. Stop it, or set ENZO_PORT."
  fi
  mkdir -p "$LOG_DIR"
  say "Starting the gateway on port $PORT (log: .dev/gateway.log)"
  (cd "$ROOT/server" && ENZO_PORT="$PORT" ENZO_DATABASE_URL="$DB_URL" exec uv run --quiet enzo-server) \
    >"$GATEWAY_LOG" 2>&1 &
  GATEWAY_PID=$!
  trap stop_gateway EXIT INT TERM
  for _ in $(seq 1 60); do
    if gateway_healthy; then
      ok "gateway is up"
      return 0
    fi
    kill -0 "$GATEWAY_PID" 2>/dev/null || break
    sleep 0.5
  done
  say "The gateway did not start. Last lines of .dev/gateway.log:"
  tail -n 20 "$GATEWAY_LOG" >&2 || true
  exit 1
}

stop_gateway() {
  if [ -n "$GATEWAY_PID" ] && kill -0 "$GATEWAY_PID" 2>/dev/null; then
    kill "$GATEWAY_PID" 2>/dev/null || true
    wait "$GATEWAY_PID" 2>/dev/null || true
    say "Stopped the gateway."
  fi
  GATEWAY_PID=""
}

usage() {
  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
}

main() {
  local mode="${1:-cli}"
  case "$mode" in
  doctor)
    check_tools
    check_database check
    stop_if_problems
    say "All good."
    ;;
  setup)
    check_tools
    check_database create
    stop_if_problems
    install_and_migrate
    ;;
  cli | web)
    check_tools
    check_database create
    stop_if_problems
    install_and_migrate
    start_gateway
    export ENZO_GATEWAY_URL="http://127.0.0.1:$PORT"
    if [ "$mode" = "web" ]; then
      say "Opening the web app: use the address Vite prints below. Ctrl+C stops everything."
      (cd "$ROOT" && pnpm --filter @enzo/web dev)
    else
      (cd "$ROOT" && pnpm --silent --filter @enzo/cli start)
    fi
    ;;
  help | -h | --help) usage ;;
  *)
    usage
    die "unknown command: $mode"
    ;;
  esac
}

main "$@"
