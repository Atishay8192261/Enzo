"""Enzo always on, on a machine the user owns (`enzo deploy`, ADR 0034).

- ``service``: on the always-on machine, Enzo as a systemd user service (Linux) or a launchd agent
  (macOS) that starts at boot and restarts when it fails (`enzo service …`).
- ``ssh``, ``probe``, ``remote``: on the user's computer, everything over their own SSH: check the
  machine, install the same Enzo there, set up the service, and show its health.
- ``tunnel``: the apps reach it through one shared SSH port forward; nothing listens beyond
  127.0.0.1 on either machine.
- ``move``: optionally copy chats, memory, goals and scheduled work there (asks first).
"""
