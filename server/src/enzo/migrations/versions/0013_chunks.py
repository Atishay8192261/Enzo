"""chunks: memories, goals, past chats and stored texts, embedded for search by meaning

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-26 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # pgvector: bundled with installed copies (pixeltable-pgserver); developers install it.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "chunks",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("part", sa.Integer(), nullable=False),
        sa.Column("memory_id", sa.BigInteger(), nullable=True),
        sa.Column("goal_id", sa.BigInteger(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("seq_from", sa.Integer(), nullable=True),
        sa.Column("seq_to", sa.Integer(), nullable=True),
        sa.Column("stored_id", sa.Text(), nullable=True),
        sa.Column("start", sa.Integer(), server_default="0", nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("untrusted", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("happened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("embedding", VECTOR(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "search",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source IN ('memory', 'goal', 'turn', 'stored')", name=op.f("ck_chunks_source")
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_chunks_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["goal_id"], ["goals.id"], name=op.f("fk_chunks_goal_id_goals"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["memory_id"],
            ["memories.id"],
            name=op.f("fk_chunks_memory_id_memories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chunks")),
        sa.UniqueConstraint("key", "part", name=op.f("uq_chunks_key")),
    )
    op.create_index(op.f("ix_chunks_conversation_id"), "chunks", ["conversation_id"], unique=False)
    op.create_index(op.f("ix_chunks_goal_id"), "chunks", ["goal_id"], unique=False)
    op.create_index(op.f("ix_chunks_memory_id"), "chunks", ["memory_id"], unique=False)
    op.create_index(
        op.f("ix_chunks_search"), "chunks", ["search"], unique=False, postgresql_using="gin"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_chunks_search"), table_name="chunks", postgresql_using="gin")
    op.drop_index(op.f("ix_chunks_memory_id"), table_name="chunks")
    op.drop_index(op.f("ix_chunks_goal_id"), table_name="chunks")
    op.drop_index(op.f("ix_chunks_conversation_id"), table_name="chunks")
    op.drop_table("chunks")
    op.execute("DROP EXTENSION IF EXISTS vector")
