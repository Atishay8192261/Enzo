"""Bring your own account: users connect their own provider credentials (decided 2026-11-17).

Secrets live only in a vault (spec §11.2): the OS keychain, or an AES-GCM encrypted file whose key
comes from ENZO_VAULT_KEY. Never plaintext, never the database, logs or transcripts.
"""
