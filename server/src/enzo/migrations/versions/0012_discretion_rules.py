"""discretion rules the user stated, and the private details an approval card names

Revision ID: 0012
Revises: 0010
Create Date: 2026-11-26 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "discretion_rules",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("kinds", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("words", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("said_in", sa.Uuid(), nullable=True),
        sa.Column("turn_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_discretion_rules_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["said_in"],
            ["conversations.id"],
            name=op.f("fk_discretion_rules_said_in_conversations"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discretion_rules")),
    )
    op.create_index(
        op.f("ix_discretion_rules_conversation_id"),
        "discretion_rules",
        ["conversation_id"],
        unique=False,
    )
    op.add_column(
        "approvals",
        sa.Column("privacy", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("approvals", "privacy")
    op.drop_index(op.f("ix_discretion_rules_conversation_id"), table_name="discretion_rules")
    op.drop_table("discretion_rules")
