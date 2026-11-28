"""people: people in the user's life, their facts and ties, tracked items, yearly reminders

Revision ID: 0017
Revises: 0015
Create Date: 2026-11-28 04:30:35.907516

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "people",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "named_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("turn_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_people_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_people")),
    )
    op.create_table(
        "person_aliases",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("person_id", sa.BigInteger(), nullable=False),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("turn_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_person_aliases_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["people.id"],
            name=op.f("fk_person_aliases_person_id_people"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_person_aliases")),
    )
    op.create_index(
        op.f("ix_person_aliases_person_id"),
        "person_aliases",
        ["person_id", sa.literal_column("lower(alias)")],
        unique=True,
    )
    op.create_table(
        "person_facts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("person_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("other_id", sa.BigInteger(), nullable=True),
        sa.Column("month", sa.Integer(), nullable=True),
        sa.Column("day", sa.Integer(), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("said_by", sa.Text(), nullable=False),
        sa.Column("happened_on", sa.Date(), nullable=True),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("replaces", sa.BigInteger(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("turn_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(kind = 'tie') = (other_id IS NOT NULL)", name=op.f("ck_person_facts_tie")
        ),
        sa.CheckConstraint(
            "kind <> 'birthday' OR (month BETWEEN 1 AND 12 AND day BETWEEN 1 AND 31)",
            name=op.f("ck_person_facts_birthday"),
        ),
        sa.CheckConstraint(
            "kind IN ('relation', 'birthday', 'likes', 'food', 'contact', 'other', 'tie')",
            name=op.f("ck_person_facts_kind"),
        ),
        sa.CheckConstraint("said_by IN ('user', 'inferred')", name=op.f("ck_person_facts_said_by")),
        sa.CheckConstraint("status IN ('active', 'replaced')", name=op.f("ck_person_facts_status")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_person_facts_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["other_id"],
            ["people.id"],
            name=op.f("fk_person_facts_other_id_people"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["people.id"],
            name=op.f("fk_person_facts_person_id_people"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["replaces"],
            ["person_facts.id"],
            name=op.f("fk_person_facts_replaces_person_facts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_person_facts")),
    )
    op.create_index(
        "ix_person_facts_one_current",
        "person_facts",
        ["person_id", "kind"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND kind IN ('relation', 'birthday')"),
    )
    op.create_index(
        "ix_person_facts_one_tie",
        "person_facts",
        ["person_id", "other_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND kind = 'tie'"),
    )
    op.create_index(op.f("ix_person_facts_other_id"), "person_facts", ["other_id"], unique=False)
    op.create_index(op.f("ix_person_facts_person_id"), "person_facts", ["person_id"], unique=False)
    op.create_index(op.f("ix_person_facts_replaces"), "person_facts", ["replaces"], unique=False)
    op.create_table(
        "tracked_items",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("person_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("due_on", sa.Date(), nullable=True),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("remains", sa.Text(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("turn_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('lent', 'borrowed', 'owed_to_me', 'i_owe', 'waiting_for', 'other')",
            name=op.f("ck_tracked_items_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'done', 'dropped')", name=op.f("ck_tracked_items_status")
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_tracked_items_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["people.id"],
            name=op.f("fk_tracked_items_person_id_people"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tracked_items")),
    )
    op.create_index(
        op.f("ix_tracked_items_person_id"), "tracked_items", ["person_id"], unique=False
    )
    op.create_index(op.f("ix_tracked_items_status"), "tracked_items", ["status"], unique=False)
    op.create_table(
        "person_links",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("person_id", sa.BigInteger(), nullable=False),
        sa.Column("goal_id", sa.BigInteger(), nullable=True),
        sa.Column("job_id", sa.BigInteger(), nullable=True),
        sa.Column("days_before", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(goal_id IS NULL) <> (job_id IS NULL)", name=op.f("ck_person_links_one")
        ),
        sa.CheckConstraint(
            "days_before IS NULL OR days_before BETWEEN 0 AND 60", name=op.f("ck_person_links_days")
        ),
        sa.ForeignKeyConstraint(
            ["goal_id"],
            ["goals.id"],
            name=op.f("fk_person_links_goal_id_goals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_person_links_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["people.id"],
            name=op.f("fk_person_links_person_id_people"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_person_links")),
        sa.UniqueConstraint("person_id", "goal_id", name="uq_person_links_goal"),
        sa.UniqueConstraint("person_id", "job_id", name="uq_person_links_job"),
    )
    op.create_index(op.f("ix_person_links_goal_id"), "person_links", ["goal_id"], unique=False)
    op.create_index(op.f("ix_person_links_job_id"), "person_links", ["job_id"], unique=False)
    op.create_index(op.f("ix_person_links_person_id"), "person_links", ["person_id"], unique=False)
    op.create_table(
        "tracked_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("change", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("basis", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("happened_on", sa.Date(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("turn_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "basis IS NULL OR basis IN ('user_confirmed', 'verified', 'timeline_ended')",
            name=op.f("ck_tracked_events_basis"),
        ),
        sa.CheckConstraint(
            "change IN ('created', 'updated', 'done', 'dropped')",
            name=op.f("ck_tracked_events_change"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_tracked_events_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["tracked_items.id"],
            name=op.f("fk_tracked_events_item_id_tracked_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tracked_events")),
    )
    op.create_index(op.f("ix_tracked_events_item_id"), "tracked_events", ["item_id"], unique=False)
    op.add_column("chunks", sa.Column("person_id", sa.BigInteger(), nullable=True))
    op.add_column("chunks", sa.Column("tracked_id", sa.BigInteger(), nullable=True))
    op.create_index(op.f("ix_chunks_person_id"), "chunks", ["person_id"], unique=False)
    op.create_index(op.f("ix_chunks_tracked_id"), "chunks", ["tracked_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_chunks_person_id_people"),
        "chunks",
        "people",
        ["person_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        op.f("fk_chunks_tracked_id_tracked_items"),
        "chunks",
        "tracked_items",
        ["tracked_id"],
        ["id"],
        ondelete="CASCADE",
    )
    # Checks are not compared by autogenerate: written by hand.
    _checks(CHUNK_SOURCES + ", 'person', 'tracked'", REPEATS + ", 'yearly'")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "UPDATE jobs SET status = 'cancelled', next_run_at = NULL, repeat = 'once' "
        "WHERE repeat = 'yearly'"
    )
    op.execute("DELETE FROM chunks WHERE source IN ('person', 'tracked')")
    _checks(CHUNK_SOURCES, REPEATS)
    op.drop_constraint(op.f("fk_chunks_tracked_id_tracked_items"), "chunks", type_="foreignkey")
    op.drop_constraint(op.f("fk_chunks_person_id_people"), "chunks", type_="foreignkey")
    op.drop_index(op.f("ix_chunks_tracked_id"), table_name="chunks")
    op.drop_index(op.f("ix_chunks_person_id"), table_name="chunks")
    op.drop_column("chunks", "tracked_id")
    op.drop_column("chunks", "person_id")
    op.drop_index(op.f("ix_tracked_events_item_id"), table_name="tracked_events")
    op.drop_table("tracked_events")
    op.drop_index(op.f("ix_person_links_person_id"), table_name="person_links")
    op.drop_index(op.f("ix_person_links_job_id"), table_name="person_links")
    op.drop_index(op.f("ix_person_links_goal_id"), table_name="person_links")
    op.drop_table("person_links")
    op.drop_index(op.f("ix_tracked_items_status"), table_name="tracked_items")
    op.drop_index(op.f("ix_tracked_items_person_id"), table_name="tracked_items")
    op.drop_table("tracked_items")
    op.drop_index(op.f("ix_person_facts_replaces"), table_name="person_facts")
    op.drop_index(op.f("ix_person_facts_person_id"), table_name="person_facts")
    op.drop_index(op.f("ix_person_facts_other_id"), table_name="person_facts")
    op.drop_index(
        "ix_person_facts_one_tie",
        table_name="person_facts",
        postgresql_where=sa.text("status = 'active' AND kind = 'tie'"),
    )
    op.drop_index(
        "ix_person_facts_one_current",
        table_name="person_facts",
        postgresql_where=sa.text("status = 'active' AND kind IN ('relation', 'birthday')"),
    )
    op.drop_table("person_facts")
    op.drop_index(op.f("ix_person_aliases_person_id"), table_name="person_aliases")
    op.drop_table("person_aliases")
    op.drop_table("people")


CHUNK_SOURCES = "'memory', 'goal', 'turn', 'stored'"
REPEATS = "'once', 'hourly', 'daily', 'weekdays', 'weekly', 'monthly'"


def _checks(sources: str, repeats: str) -> None:
    op.drop_constraint(op.f("ck_chunks_source"), "chunks", type_="check")
    op.create_check_constraint(op.f("ck_chunks_source"), "chunks", f"source IN ({sources})")
    op.drop_constraint(op.f("ck_jobs_repeat"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_repeat"), "jobs", f"repeat IN ({repeats})")
