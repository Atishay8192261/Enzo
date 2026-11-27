"""hooks: watches that check without a model call and wake Enzo on a change

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-26 16:24:15.536949

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "hooks",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("what", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("target", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("rule", sa.Text(), nullable=False),
        sa.Column("threshold", sa.Numeric(), nullable=True),
        sa.Column("match", sa.Text(), nullable=True),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("interval_s", sa.Integer(), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_wakes", sa.Integer(), nullable=True),
        sa.Column("wakes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("polls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="off", nullable=False),
        sa.Column("stop_reason", sa.Text(), nullable=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("shown", sa.Text(), nullable=True),
        sa.Column("number", sa.Numeric(), nullable=True),
        sa.Column("matched", sa.Boolean(), nullable=True),
        sa.Column("value_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dry_value", sa.Text(), nullable=True),
        sa.Column("dry_shown", sa.Text(), nullable=True),
        sa.Column("dry_number", sa.Numeric(), nullable=True),
        sa.Column("dry_matched", sa.Boolean(), nullable=True),
        sa.Column("dry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("polling_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Uuid(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("turn_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "rule IN ('changes', 'new', 'above', 'below', 'appears')", name=op.f("ck_hooks_rule")
        ),
        sa.CheckConstraint(
            "source IN ('page', 'email', 'calendar', 'pull_request')", name=op.f("ck_hooks_source")
        ),
        sa.CheckConstraint(
            "status IN ('off', 'on', 'stopped', 'done', 'cancelled')", name=op.f("ck_hooks_status")
        ),
        sa.CheckConstraint(
            "ends_at IS NOT NULL OR max_wakes IS NOT NULL", name=op.f("ck_hooks_stop")
        ),
        sa.CheckConstraint("interval_s >= 60", name=op.f("ck_hooks_interval")),
        sa.ForeignKeyConstraint(
            ["chat_id"],
            ["conversations.id"],
            name=op.f("fk_hooks_chat_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_hooks_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hooks")),
    )
    op.create_index(op.f("ix_hooks_scope"), "hooks", ["scope"], unique=False)
    op.create_index(op.f("ix_hooks_status"), "hooks", ["status", "next_poll_at"], unique=False)
    op.create_table(
        "hook_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("hook_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("before", sa.Text(), nullable=True),
        sa.Column("after", sa.Text(), nullable=True),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.Text(), server_default="pending", nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("late", sa.Boolean(), server_default="false", nullable=False),
        sa.CheckConstraint("kind IN ('wake', 'stopped')", name=op.f("ck_hook_events_kind")),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('reported', 'needs_approval', 'failed', 'stopped')",
            name=op.f("ck_hook_events_outcome"),
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'done')", name=op.f("ck_hook_events_state")
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_hook_events_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["hook_id"], ["hooks.id"], name=op.f("fk_hook_events_hook_id_hooks"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hook_events")),
    )
    op.create_index(
        op.f("ix_hook_events_conversation_id"), "hook_events", ["conversation_id"], unique=False
    )
    op.create_index(op.f("ix_hook_events_hook_id"), "hook_events", ["hook_id"], unique=False)
    op.create_index(op.f("ix_hook_events_state"), "hook_events", ["state"], unique=False)
    # A watch's wake starts a turn in its own chat: the apps show it as a watch, not as typing.
    op.drop_constraint(op.f("ck_messages_origin"), "messages", type_="check")
    op.create_check_constraint(
        op.f("ck_messages_origin"),
        "messages",
        "origin IN ('user', 'schedule', 'brief', 'handoff', 'watch')",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("ck_messages_origin"), "messages", type_="check")
    op.execute("UPDATE messages SET origin = 'schedule' WHERE origin = 'watch'")
    op.create_check_constraint(
        op.f("ck_messages_origin"), "messages", "origin IN ('user', 'schedule', 'brief', 'handoff')"
    )
    op.drop_index(op.f("ix_hook_events_state"), table_name="hook_events")
    op.drop_index(op.f("ix_hook_events_hook_id"), table_name="hook_events")
    op.drop_index(op.f("ix_hook_events_conversation_id"), table_name="hook_events")
    op.drop_table("hook_events")
    op.drop_index(op.f("ix_hooks_status"), table_name="hooks")
    op.drop_index(op.f("ix_hooks_scope"), table_name="hooks")
    op.drop_table("hooks")
