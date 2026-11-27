"""helpers: work handed to helpers in their own chats, their reports and notices

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-26 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "helpers",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("root_id", sa.Uuid(), nullable=True),
        sa.Column("chat_id", sa.Uuid(), nullable=True),
        sa.Column("coordinator_id", sa.BigInteger(), nullable=True),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("brief", sa.Text(), nullable=False),
        sa.Column("request", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="working", nullable=False),
        sa.Column("report", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("restarts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("stopped_by", sa.Text(), nullable=True),
        sa.Column("turn_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('working', 'waiting', 'done', 'failed', 'stopped', 'interrupted')",
            name=op.f("ck_helpers_status"),
        ),
        sa.CheckConstraint("depth IN (1, 2)", name=op.f("ck_helpers_depth")),
        sa.CheckConstraint("restarts >= 0", name=op.f("ck_helpers_restarts")),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            ["conversations.id"],
            name=op.f("fk_helpers_chat_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["coordinator_id"],
            ["helpers.id"],
            name=op.f("fk_helpers_coordinator_id_helpers"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["conversations.id"],
            name=op.f("fk_helpers_parent_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["root_id"],
            ["conversations.id"],
            name=op.f("fk_helpers_root_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_helpers")),
        sa.UniqueConstraint("chat_id", name=op.f("uq_helpers_chat_id")),
    )
    op.create_index(op.f("ix_helpers_coordinator_id"), "helpers", ["coordinator_id"], unique=False)
    op.create_index(op.f("ix_helpers_parent_id"), "helpers", ["parent_id"], unique=False)
    op.create_index(op.f("ix_helpers_root_id"), "helpers", ["root_id"], unique=False)
    op.create_index(op.f("ix_helpers_status"), "helpers", ["status"], unique=False)
    op.create_table(
        "helper_notices",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("helper_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("after", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('started', 'waiting', 'done', 'failed', 'stopped', 'interrupted', 'resumed')",
            name=op.f("ck_helper_notices_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_helper_notices_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["helper_id"],
            ["helpers.id"],
            name=op.f("fk_helper_notices_helper_id_helpers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_helper_notices")),
    )
    op.create_index(
        op.f("ix_helper_notices_conversation_id"),
        "helper_notices",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_helper_notices_helper_id"), "helper_notices", ["helper_id"], unique=False
    )
    # A helper's brief and helpers' reports are messages the user did not type.
    op.drop_constraint(op.f("ck_messages_origin"), "messages", type_="check")
    op.create_check_constraint(
        op.f("ck_messages_origin"),
        "messages",
        "origin IN ('user', 'schedule', 'brief', 'handoff')",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("ck_messages_origin"), "messages", type_="check")
    op.execute("UPDATE messages SET origin = 'user' WHERE origin IN ('brief', 'handoff')")
    op.create_check_constraint(
        op.f("ck_messages_origin"), "messages", "origin IN ('user', 'schedule')"
    )
    op.drop_index(op.f("ix_helper_notices_helper_id"), table_name="helper_notices")
    op.drop_index(op.f("ix_helper_notices_conversation_id"), table_name="helper_notices")
    op.drop_table("helper_notices")
    op.drop_index(op.f("ix_helpers_status"), table_name="helpers")
    op.drop_index(op.f("ix_helpers_root_id"), table_name="helpers")
    op.drop_index(op.f("ix_helpers_parent_id"), table_name="helpers")
    op.drop_index(op.f("ix_helpers_coordinator_id"), table_name="helpers")
    op.drop_table("helpers")
