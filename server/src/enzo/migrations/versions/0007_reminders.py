"""reminders: things Enzo does at a time for the user, and each delivery of them

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-25 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("turn_id", sa.Text(), nullable=True),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("first_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("repeat", sa.Text(), server_default="once", nullable=False),
        sa.Column("every", sa.Integer(), server_default="1", nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("max_runs", sa.Integer(), nullable=True),
        sa.Column("runs", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("kind IN ('reminder')", name=op.f("ck_jobs_kind")),
        sa.CheckConstraint(
            "repeat IN ('once', 'hourly', 'daily', 'weekdays', 'weekly', 'monthly')",
            name=op.f("ck_jobs_repeat"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'done', 'cancelled')", name=op.f("ck_jobs_status")
        ),
        sa.CheckConstraint("every >= 1", name=op.f("ck_jobs_every")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_jobs_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index(op.f("ix_jobs_status"), "jobs", ["status", "next_run_at"])
    op.create_table(
        "job_runs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("late", sa.Boolean(), nullable=False),
        sa.Column("after", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_job_runs_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_job_runs_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_runs")),
    )
    op.create_index(op.f("ix_job_runs_job_id"), "job_runs", ["job_id"])
    op.create_index(op.f("ix_job_runs_conversation_id"), "job_runs", ["conversation_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_job_runs_conversation_id"), table_name="job_runs")
    op.drop_index(op.f("ix_job_runs_job_id"), table_name="job_runs")
    op.drop_table("job_runs")
    op.drop_index(op.f("ix_jobs_status"), table_name="jobs")
    op.drop_table("jobs")
