# ADR 0005 — Persistence base: Postgres, SQLAlchemy, Alembic

- **Status:** Accepted
- **Date:** 2026-11-17

## Decisions

| # | Decision | Why |
|---|---|---|
| 1 | Postgres 16 + pgvector is the one durable store (records now, vectors next). | §11.2 structured records, §6.3 vector memory, and later the job queue, in one transactional store. |
| 2 | SQLAlchemy 2.1 (async, asyncpg) with typed declarative models; Alembic migrations generated from them, reviewed, and auto-formatted by a post-write hook. | Typed, standard tooling. A test proves models and migrations describe the same schema; another proves upgrade → downgrade → upgrade. |
| 3 | A turn is saved in one transaction, with the conversation row locked (`FOR UPDATE`). | All-or-nothing turns; concurrent turns cannot collide on message order (tested with 10 concurrent saves). |
| 4 | Messages are stored as canonical `Message` JSON (JSONB), including native replay data. | Exact round-trip, so a stored conversation resumes on any brain (§6.1). |
| 5 | Approvals are records keyed by (conversation, approval id); a decision is set once and never overwritten; only decided records count. | §12.2: the approval record is what matters; a claim of past consent is not consent. |
| 6 | Unknown cost is `NULL`, never 0. | §1.3.5. |
| 7 | Tests use a real Postgres (fresh database per session, migrated with Alembic) and fail loudly when it is missing. | No mocks for the store; CI installs the same packages as local and cloud setup. |

## Evidence found while building

- Alembic's `fileConfig` disables every existing logger by default, which silenced Enzo's routing logs
  whenever migrations ran first. Fixed with `disable_existing_loggers=False` and an order-independent
  regression test that fails without the fix.
