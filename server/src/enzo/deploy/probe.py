"""What the always-on machine is, checked over SSH before anything is installed there.

One POSIX sh script (any login shell runs it: it goes to ``sh -s``) prints ``key=value`` lines;
it changes nothing. :func:`problems` says what stops Enzo from running there, :func:`notes` what
is worth knowing.
"""

from dataclasses import dataclass

#: Enzo's database alone needs a few hundred MB; Python, the browser and the chats need more.
MIN_DISK_GB = 2.0
LOW_MEMORY_GB = 1.0
ARCHES = {"x86_64", "amd64", "aarch64", "arm64"}

SCRIPT = r"""
set +e
echo "os=$(uname -s)"
echo "arch=$(uname -m)"
echo "user=$(id -un)"
echo "uid=$(id -u)"
echo "home=$HOME"
df -Pk "$HOME" 2>/dev/null | awk 'NR==2 {print "disk_kb=" $4}'
command -v curl >/dev/null 2>&1 && echo "curl=1"
uv=""
for c in "$(command -v uv 2>/dev/null)" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
  if [ -n "$c" ] && [ -x "$c" ]; then uv=$c; break; fi
done
if [ -n "$uv" ]; then
  echo "uv=$uv"
  bin=$("$uv" tool dir --bin 2>/dev/null)
  if [ -n "$bin" ] && [ -x "$bin/enzo" ]; then
    echo "enzo=$bin/enzo"
    echo "enzo_version=$("$bin/enzo" --version 2>/dev/null | awk '{print $2}')"
  fi
fi
case "$(uname -s)" in
  Linux)
    awk '/^MemTotal:/ {print "mem_kb=" $2}' /proc/meminfo 2>/dev/null
    [ -r /etc/os-release ] && . /etc/os-release && echo "distro=$PRETTY_NAME"
    getconf GNU_LIBC_VERSION >/dev/null 2>&1 && echo "glibc=1"
    if command -v systemctl >/dev/null 2>&1; then
      echo "systemd=1"
      run="/run/user/$(id -u)"
      [ -z "$XDG_RUNTIME_DIR" ] && [ -d "$run" ] && export XDG_RUNTIME_DIR="$run"
      systemctl --user show-environment >/dev/null 2>&1 && echo "systemd_user=1"
    fi
    echo "linger=$(loginctl show-user "$(id -un)" --property=Linger --value 2>/dev/null)"
    ;;
  Darwin)
    echo "mem_kb=$(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1024 ))"
    echo "distro=macOS $(sw_vers -productVersion 2>/dev/null)"
    launchctl print "gui/$(id -u)" >/dev/null 2>&1 && echo "gui=1"
    pmset -g 2>/dev/null | awk '$1 == "sleep" {print "sleep=" $2}'
    ;;
esac
exit 0
"""


@dataclass(frozen=True, slots=True)
class Host:
    os: str
    arch: str
    user: str
    home: str
    disk_gb: float | None
    mem_gb: float | None
    distro: str
    curl: bool
    uv: str | None
    enzo: str | None
    enzo_version: str | None
    glibc: bool
    systemd: bool
    systemd_user: bool
    linger: bool
    gui: bool
    #: macOS: minutes before the Mac sleeps on its own (0: never), when known.
    sleep_minutes: int | None

    @property
    def summary(self) -> str:
        """ "Ubuntu 24.04.1 LTS, x86_64, 38 GB free"."""
        free = f", {self.disk_gb:g} GB free" if self.disk_gb is not None else ""
        return f"{self.distro or self.os}, {self.arch}{free}"


def parse(out: str) -> Host:
    values: dict[str, str] = {}
    for line in out.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.isidentifier():
            values[key] = value.strip()

    def gb(key: str) -> float | None:
        raw = values.get(key, "")
        return round(int(raw) / 1024 / 1024, 1) if raw.isdigit() else None

    sleep = values.get("sleep", "")
    return Host(
        os=values.get("os", ""),
        arch=values.get("arch", ""),
        user=values.get("user", ""),
        home=values.get("home", ""),
        disk_gb=gb("disk_kb"),
        mem_gb=gb("mem_kb"),
        distro=values.get("distro", ""),
        curl=values.get("curl") == "1",
        uv=values.get("uv") or None,
        enzo=values.get("enzo") or None,
        enzo_version=values.get("enzo_version") or None,
        glibc=values.get("glibc") == "1",
        systemd=values.get("systemd") == "1",
        systemd_user=values.get("systemd_user") == "1",
        linger=values.get("linger") == "yes",
        gui=values.get("gui") == "1",
        sleep_minutes=int(sleep) if sleep.isdigit() else None,
    )


def problems(host: Host) -> list[str]:
    """What stops Enzo from running there, each with what to do."""
    found: list[str] = []
    if host.os not in ("Linux", "Darwin"):
        found.append(f"It runs {host.os or 'an unknown system'}: Enzo runs on Linux and macOS.")
        return found
    if host.arch not in ARCHES:
        found.append(f"Its {host.arch} processor isn't supported yet (x64 and arm64 are).")
    if host.os == "Linux":
        if not host.glibc:
            found.append(
                "It uses musl (like Alpine Linux): Enzo's database needs glibc (Ubuntu, Debian, "
                "Fedora…)."
            )
        if not host.systemd:
            found.append("It doesn't use systemd, which Enzo needs to stay on there.")
        elif not host.systemd_user:
            found.append(
                "Its systemd user services aren't reachable over SSH. Make sure you log in as "
                "this user directly (not su or sudo) and that the system runs systemd-logind."
            )
    if host.os == "Darwin" and not host.gui:
        found.append(
            "Nobody is logged in at this Mac's screen, and a Mac starts your background apps only "
            "then. Log in there once, and turn on automatic login (System Settings → Users & "
            "Groups) so Enzo starts after a restart too."
        )
    if host.disk_gb is not None and host.disk_gb < MIN_DISK_GB:
        found.append(f"Only {host.disk_gb:g} GB is free there; Enzo needs {MIN_DISK_GB:g} GB.")
    if host.uv is None and not host.curl:
        found.append("It has neither uv nor curl, so Enzo's installer can't run there.")
    return found


def notes(host: Host, version: str) -> list[str]:
    found: list[str] = []
    if host.user == "root":
        found.append(
            "You're signing in as root. Enzo works, but it never needs root: a normal user "
            "account is safer."
        )
    if host.mem_gb is not None and host.mem_gb < LOW_MEMORY_GB:
        found.append(f"It has {host.mem_gb:g} GB of memory; Enzo is happier with 2 GB or more.")
    if host.os == "Darwin" and host.sleep_minutes:
        found.append(
            f"This Mac goes to sleep after {host.sleep_minutes} minutes, and Enzo can't work while "
            "it sleeps. Turn on “Prevent automatic sleeping when the display is off” (System "
            "Settings → Energy)."
        )
    if host.enzo_version and host.enzo_version != version:
        found.append(f"Enzo {host.enzo_version} is installed there; it will become {version}.")
    return found
