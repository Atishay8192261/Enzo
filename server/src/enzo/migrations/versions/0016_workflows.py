"""workflows: runs of code-defined plans, their steps (each a helper's turn) and notices

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-28 04:30:42.158958

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("workflow", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("started_by", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="running", nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("stopped_by", sa.Text(), nullable=True),
        sa.Column("restarts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("turn_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "started_by IN ('user', 'enzo', 'schedule')", name=op.f("ck_workflow_runs_started_by")
        ),
        sa.CheckConstraint(
            "status IN ('running', 'waiting', 'done', 'failed', 'stopped', 'interrupted')",
            name=op.f("ck_workflow_runs_status"),
        ),
        sa.CheckConstraint("restarts >= 0", name=op.f("ck_workflow_runs_restarts")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_workflow_runs_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_runs")),
    )
    op.create_index(
        op.f("ix_workflow_runs_conversation_id"), "workflow_runs", ["conversation_id"], unique=False
    )
    op.create_index(op.f("ix_workflow_runs_status"), "workflow_runs", ["status"], unique=False)
    op.create_table(
        "workflow_notices",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("after", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('started', 'waiting', 'resumed', 'done', 'failed', 'stopped', 'interrupted')",
            name=op.f("ck_workflow_notices_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_workflow_notices_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_workflow_notices_run_id_workflow_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_notices")),
    )
    op.create_index(
        op.f("ix_workflow_notices_conversation_id"),
        "workflow_notices",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_workflow_notices_run_id"), "workflow_notices", ["run_id"], unique=False
    )
    op.create_table(
        "workflow_steps",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("item_index", sa.Integer(), nullable=False),
        sa.Column("step", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("item", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("input", sa.Text(), nullable=True),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("tools", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ask", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("brains", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("models", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tool_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=14, scale=8), nullable=True),
        sa.Column("plan", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'working', 'waiting', 'done', 'failed', 'stopped', "
            "'interrupted', 'skipped')",
            name=op.f("ck_workflow_steps_status"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_workflow_steps_run_id_workflow_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_steps")),
        sa.UniqueConstraint(
            "run_id", "step_index", "item_index", name=op.f("uq_workflow_steps_run_id")
        ),
    )
    op.create_index(op.f("ix_workflow_steps_run_id"), "workflow_steps", ["run_id"], unique=False)
    # A helper that does a workflow step: its tools, its own approvals, its step limit.
    op.add_column("helpers", sa.Column("step_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "helpers", sa.Column("tools", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.add_column(
        "helpers", sa.Column("ask", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.add_column("helpers", sa.Column("max_steps", sa.Integer(), nullable=True))
    op.create_unique_constraint(op.f("uq_helpers_step_id"), "helpers", ["step_id"])
    op.create_foreign_key(
        op.f("fk_helpers_step_id_workflow_steps"),
        "helpers",
        "workflow_steps",
        ["step_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("fk_helpers_step_id_workflow_steps"), "helpers", type_="foreignkey")
    op.drop_constraint(op.f("uq_helpers_step_id"), "helpers", type_="unique")
    op.drop_column("helpers", "max_steps")
    op.drop_column("helpers", "ask")
    op.drop_column("helpers", "tools")
    op.drop_column("helpers", "step_id")
    op.drop_index(op.f("ix_workflow_steps_run_id"), table_name="workflow_steps")
    op.drop_table("workflow_steps")
    op.drop_index(op.f("ix_workflow_notices_run_id"), table_name="workflow_notices")
    op.drop_index(op.f("ix_workflow_notices_conversation_id"), table_name="workflow_notices")
    op.drop_table("workflow_notices")
    op.drop_index(op.f("ix_workflow_runs_status"), table_name="workflow_runs")
    op.drop_index(op.f("ix_workflow_runs_conversation_id"), table_name="workflow_runs")
    op.drop_table("workflow_runs")
