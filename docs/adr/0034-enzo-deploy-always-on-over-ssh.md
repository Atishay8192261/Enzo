# ADR 0034 — `enzo deploy`: the same private Enzo, always on, over the user's own SSH

- **Status:** Accepted for the build (2026-11-27). **Open for Atishay:** phone access (below).
- **Decision it follows:** 2026-11-18 ("24/7 later, still single-user: `enzo deploy` runs the same
  private Enzo on an always-on machine the user owns… A hosted service for everyone is not planned").
- **Spec:** §9.1, §9.2 (persistent runtime, recovery after restart, supervision and startup check),
  §11.2 (secrets only in a vault), §12. Amends ADR 0009 (vault key file) and builds on 0013/0026
  (loopback sign-in), 0016 (installer, launcher, bundled Postgres), 0019 (logs), 0021 (background).
- **How to use and test it:** `docs/ops/deploy.md`.

## Decision

1. **Over the user's own SSH, nothing else.** `enzo deploy user@host` runs the system `ssh` with
   `BatchMode=yes`: their keys or ssh-agent, their `~/.ssh/config`. Enzo never asks for, sees or
   stores a password or passphrase. A machine whose identity the user hasn't confirmed is refused
   with "connect once with `ssh user@host`", never accepted silently; a changed identity is refused
   as a possible man in the middle. Scripts go to `sh -s` on stdin, so any login shell works.
2. **Check, then install the same Enzo.** One read-only probe: OS, CPU, glibc, disk (≥ 2 GB),
   memory, systemd user services or a macOS login session, uv/curl, an existing Enzo, a Mac that
   sleeps. Then Enzo's own `install.sh` (packaged in the wheel, byte-identical to the repo's, a test
   checks) runs there with `ENZO_PACKAGE=enzo-agent==<this version>`, so both ends run the same
   version; `--package FILE.whl` sends a wheel built here (testing). `enzo` refuses to talk to a
   different version and names the fix (`enzo deploy update` or `enzo update`).
3. **An always-on service, no root** (`enzo.deploy.service`, `enzo service …` on that machine):
   - Linux: `~/.config/systemd/user/enzo.service` (`Restart=on-failure`, `WantedBy=default.target`,
     `KillMode=mixed` so the gateway stops its own Postgres, `UMask=0077`) plus
     `loginctl enable-linger`, so it starts at boot and survives logout. If lingering can't be
     turned on (tried directly, then `sudo -n` only: never a password prompt), the deploy fails with
     the one command to run.
   - macOS: `~/Library/LaunchAgents/enzo.gateway.plist` (`RunAtLoad`, `KeepAlive` on failure),
     loaded into the login session; it needs someone logged in (automatic login for restarts).
   - `enzo serve --service` logs to Enzo's file only and never stops on its own. The deploy waits for
     `/healthz` on the machine, then again through the tunnel. On that machine, `enzo` attaches to
     the service and never starts a second gateway on the same database.
4. **Nothing exposed.** The gateway listens on 127.0.0.1 there. The apps here use one shared
   `ssh -N -L 127.0.0.1:P:127.0.0.1:P` (same port number on both ends), started by the first app,
   stopped by the last, restarted by a keeper while an app is open (laptop sleep, network change).
5. **Accounts: sign in again there, through the tunnel (chosen over moving the vault).**
   - ChatGPT and Google send the browser back to `127.0.0.1:<port>/auth/…`; through the forward that
     reaches the gateway there (checked live: the sign-in link, the redirect URI and the callback).
   - Why not copy the vault: ChatGPT refresh tokens rotate (two copies revoke each other), OpenAI's
     flow is per installation (`ext_agent_host_id`), the laptop's keychain secrets would have to
     leave it, and an export path is one more way to leak them. API keys typed in `/login` travel
     inside SSH straight into the remote vault.
   - The remote vault: a machine that starts Enzo at boot has no unlocked keychain, so a fresh one
     gets `~/.config/enzo/vault.key` (32 random bytes, 0600, made there, never sent anywhere). The
     vault reads it after `ENZO_VAULT_KEY` and before the keychain, and refuses a key file others can
     read. A machine that already keeps accounts in its keychain is left as it is (and told).
     Limit: like any unattended service, whoever controls that user account can read both key and
     vault; sealing the key with systemd-creds/TPM is possible later where available.
6. **Chats and memory: copied only on yes** (`--move`, or the question; `enzo deploy move` later):
   `pg_dump` here → inside SSH → `pg_restore --single-transaction` there, plus the stored long
   results (`data/context`) and `Memory.md`/`Goals.md`. Accounts, the browser profile (cookies are
   sign-ins) and logs never go. Refused if Enzo there already has chats. Afterwards the reminders and
   tasks here are turned off, so nothing arrives twice.
7. **Once deployed, `enzo`, `enzo -c` and `enzo web` open the always-on Enzo** (`--local` opens this
   computer's own; `enzo connect`/`--remote HOST` name it). One always-on Enzo per person. If it
   can't be reached, `enzo` says so and never falls back silently (two copies would drift apart).
8. **Commands:** `enzo deploy status | logs [-f] | update | move | stop | start | remove
   [--delete-data]`; `enzo doctor` adds a line for the always-on Enzo. `stop` keeps it off across
   restarts; `remove` keeps its data unless `--delete-data` (asked first).
9. **Notifications:** reminders and tasks run there 24/7 and reach every connected app live
   (`/events` through the tunnel); the machine has no desktop to notify.

## Phone access (decision for Atishay; only SSH is built)

| | A. None (SSH from computers only) | B. Tailscale | C. Cloudflare Tunnel |
| --- | --- | --- | --- |
| Reach from a phone | No | Yes: the phone joins the user's tailnet, opens `http://enzo-box:8789` | Yes: a public `https://enzo.example.com` |
| Exposure | Nothing | Only the user's own devices (WireGuard); nothing public | Public URL: needs Cloudflare Access + Enzo's own login in front |
| Third party | None | Tailscale's coordination server (sees device names, not traffic) | Cloudflare terminates TLS: sees every request |
| Setup | — | Install Tailscale on both, one login | Domain on Cloudflare, `cloudflared`, Access policy |
| Enzo changes | — | Gateway on the tailnet address + `allowed_hosts` + a token (other tailnet devices) | Bearer token in the web app, Access, rebinding rules |

**Recommendation: B, later.** It keeps "nothing public": only the user's own devices reach Enzo, no
domain, no TLS certificates to manage, works behind home NAT. It needs a gateway token first (the web
app does not send one yet). C makes a private agent with the user's mail and accounts a public
endpoint; only with Access and never by default.

## Consequences and limits

- Tested here against a real `sshd` (127.0.0.1:2222, key login) with fake `systemctl`/`loginctl`/
  `launchctl` (the container has no systemd). A real VM, a home Mac (launchd) and real sign-ins are
  checked on the laptop (`docs/ops/deploy.md`).
- Windows and musl (Alpine) are refused with a reason. A Mac must stay logged in and awake.
- A reminder that fires while no app is connected is in its chat (and `enzo deploy status`); it is
  not replayed to an app that connects later. Replaying missed events is the next step.
- Other accounts on the always-on machine can reach its 127.0.0.1 port, as on a laptop; a token or
  a Unix socket (`ssh -L` to a socket) would close that.
