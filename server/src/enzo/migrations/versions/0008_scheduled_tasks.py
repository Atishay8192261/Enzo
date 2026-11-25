"""scheduled tasks: agent runs on a schedule, each run's outcome, and messages a run started

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-25 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(op.f("ck_jobs_kind"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_kind"), "jobs", "kind IN ('reminder', 'task')")
    op.add_column("jobs", sa.Column("stop_when", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("chat_id", sa.Uuid(), nullable=True))
    op.add_column("jobs", sa.Column("running_since", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("failures", sa.Integer(), server_default="0", nullable=False))
    op.create_foreign_key(
        op.f("fk_jobs_chat_id_conversations"),
        "jobs",
        "conversations",
        ["chat_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "job_runs", sa.Column("outcome", sa.Text(), server_default="delivered", nullable=False)
    )
    op.create_check_constraint(
        op.f("ck_job_runs_outcome"),
        "job_runs",
        "outcome IN ('delivered', 'reported', 'quiet', 'finished', 'needs_approval', 'failed')",
    )
    op.add_column("messages", sa.Column("origin", sa.Text(), server_default="user", nullable=False))
    op.create_check_constraint(
        op.f("ck_messages_origin"), "messages", "origin IN ('user', 'schedule')"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("ck_messages_origin"), "messages", type_="check")
    op.drop_column("messages", "origin")
    op.drop_constraint(op.f("ck_job_runs_outcome"), "job_runs", type_="check")
    op.drop_column("job_runs", "outcome")
    op.drop_constraint(op.f("fk_jobs_chat_id_conversations"), "jobs", type_="foreignkey")
    op.drop_column("jobs", "failures")
    op.drop_column("jobs", "running_since")
    op.drop_column("jobs", "chat_id")
    op.drop_column("jobs", "stop_when")
    op.execute("DELETE FROM jobs WHERE kind = 'task'")
    op.drop_constraint(op.f("ck_jobs_kind"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_kind"), "jobs", "kind IN ('reminder')")
