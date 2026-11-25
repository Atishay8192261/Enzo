"""compactions: summaries of a chat's older turns, replacing them in the prompt

Revision ID: 0010
Revises: 0008
Create Date: 2026-11-25 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "compactions",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("turns", sa.Integer(), nullable=False),
        sa.Column("made_turn", sa.Integer(), nullable=False),
        sa.Column("made_offset", sa.Integer(), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("brain", sa.Text(), nullable=False),
        sa.Column("tokens_before", sa.Integer(), nullable=False),
        sa.Column("tokens_after", sa.Integer(), nullable=False),
        sa.Column("turn_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "turns >= 1 AND made_turn >= turns AND made_offset >= 0",
            name=op.f("ck_compactions_span"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_compactions_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_compactions")),
    )
    op.create_index(
        op.f("ix_compactions_conversation_id"), "compactions", ["conversation_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_compactions_conversation_id"), table_name="compactions")
    op.drop_table("compactions")
