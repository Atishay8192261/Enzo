"""memory: durable facts about the user, each with where it came from and what it replaces

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-20 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "memories",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("about", sa.Text(), nullable=True),
        sa.Column("said_by", sa.Text(), nullable=False),
        sa.Column("happened_on", sa.Date(), nullable=True),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("replaces", sa.BigInteger(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("turn_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "search",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('english', coalesce(about, '') || ' ' || text)", persisted=True
            ),
            nullable=True,
        ),
        sa.CheckConstraint(
            "kind IN ('about', 'preference', 'person', 'decision', 'fact')",
            name=op.f("ck_memories_kind"),
        ),
        sa.CheckConstraint("said_by IN ('user', 'inferred')", name=op.f("ck_memories_said_by")),
        sa.CheckConstraint("status IN ('active', 'replaced')", name=op.f("ck_memories_status")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_memories_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["replaces"],
            ["memories.id"],
            name=op.f("fk_memories_replaces_memories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memories")),
    )
    op.create_index(op.f("ix_memories_replaces"), "memories", ["replaces"])
    op.create_index(op.f("ix_memories_search"), "memories", ["search"], postgresql_using="gin")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_memories_search"), table_name="memories", postgresql_using="gin")
    op.drop_index(op.f("ix_memories_replaces"), table_name="memories")
    op.drop_table("memories")
