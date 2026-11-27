# Enzo always on: `enzo deploy`

Run your own Enzo on a machine that never sleeps (a cloud VM or a home computer you own), so
reminders and scheduled tasks keep working while your laptop is closed. Decisions: ADR 0034.

## Before you start

- A Linux machine with systemd (Ubuntu, Debian, Fedora…; x64 or arm64, 2 GB free disk, 2 GB memory
  is comfortable) or a Mac that stays logged in and awake.
- SSH key login to it: `ssh-copy-id me@myserver`, then `ssh me@myserver` must work without a
  password. Enzo uses your keys or ssh-agent and never asks for a password.
- A normal user account is safer than root (Enzo never needs root).

## Use

```bash
enzo deploy me@myserver        # check it, install this exact version, start it, ask to copy chats
enzo                           # now opens the always-on Enzo (through SSH); --local: this computer's
enzo web                       # the web app, through the same SSH tunnel
enzo deploy status             # running? version, accounts, next reminder, disk
enzo deploy logs -f            # its log
enzo deploy update             # after `enzo update` here: the same version there
enzo deploy move               # copy chats and memory there later
enzo deploy stop | start       # off (stays off after a restart) / on again
enzo deploy remove             # remove it (keeps its data; --delete-data deletes it, asked first)
```

Then sign in there once: in `enzo`, `/login chatgpt` (or `/login anthropic`, `/login google`…).
The sign-in page opens in your laptop's browser and comes back through SSH.

Nothing listens on the network: the gateway there and the tunnel here use 127.0.0.1 only
(port 8789; `--port` changes it on both ends).

## On that machine

- Linux: `systemctl --user status enzo` · `journalctl --user -u enzo` · linger must be on
  (`loginctl show-user $USER -p Linger`; fix: `sudo loginctl enable-linger $USER`).
- macOS: `launchctl print gui/$(id -u)/enzo.gateway`; System Settings → General → Login Items lists
  it. Turn on automatic login and "Prevent automatic sleeping when the display is off".
- `enzo service status` there says the same as `enzo deploy status` here.

## Laptop test (real VM or home machine)

1. Laptop: `enzo update` (or a build: `cd server && uv build --wheel`), `enzo doctor`.
2. A VM (e.g. a small Ubuntu 24.04 cloud VM) or a home Linux box with your SSH key; `ssh me@vm`
   works. First `ssh` confirms its identity.
3. `enzo deploy me@vm` (from a build: `--package server/dist/enzo_agent-<v>-py3-none-any.whl`).
   Expect ✓ lines: the machine, installed, running (systemd, starts at boot), reachable through SSH.
   Answer y to copy chats: then `enzo --local` shows your reminders turned off here.
4. `enzo` → header "connected"; `/login chatgpt` → the browser signs in and comes back ("Signed in
   to Enzo"); ask something; `enzo deploy status` lists the account.
5. "remind me in 5 minutes to stand up", quit `enzo`, close the laptop lid 10 minutes, reopen:
   `enzo deploy status` (next reminder gone), `enzo -c` shows "⏰ Reminder: Stand up" in the chat.
6. Reboot the VM (`ssh me@vm sudo reboot`), wait a minute: `enzo deploy status` is running again
   without logging in (linger). `ssh me@vm 'kill -9 $(pgrep -f "enzo serve")'`: running again
   within ~10 s (restart on failure), `restarted 1 times`.
7. Optional, a home Mac: same steps with `me@mac.local`; check it says "starts at login".
8. `enzo deploy remove` → `enzo` opens the laptop's own Enzo again.

File an issue (label `user-testing`) for any step that fails, with `enzo deploy status` and
`enzo deploy logs --json -n 100`.
