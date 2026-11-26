"""goals: structured goals with steps, the evidence for each change, and check-ins linked to them

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-25 19:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "goals",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("due_on", sa.Date(), nullable=True),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("turn_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('open', 'done', 'dropped')", name=op.f("ck_goals_status")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_goals_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_goals")),
    )
    op.create_index(op.f("ix_goals_status"), "goals", ["status"], unique=False)
    op.create_table(
        "goal_steps",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("goal_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["goal_id"], ["goals.id"], name=op.f("fk_goal_steps_goal_id_goals"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_goal_steps")),
        sa.UniqueConstraint("goal_id", "position", name=op.f("uq_goal_steps_goal_id")),
    )
    op.create_table(
        "goal_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("goal_id", sa.BigInteger(), nullable=False),
        sa.Column("change", sa.Text(), nullable=False),
        sa.Column("step_id", sa.BigInteger(), nullable=True),
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
            name=op.f("ck_goal_events_basis"),
        ),
        sa.CheckConstraint(
            "change IN ('created', 'steps_added', 'step_done', 'done', 'dropped')",
            name=op.f("ck_goal_events_change"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_goal_events_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["goal_id"], ["goals.id"], name=op.f("fk_goal_events_goal_id_goals"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["step_id"],
            ["goal_steps.id"],
            name=op.f("fk_goal_events_step_id_goal_steps"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_goal_events")),
    )
    op.create_index(op.f("ix_goal_events_goal_id"), "goal_events", ["goal_id"], unique=False)
    op.add_column("jobs", sa.Column("goal_id", sa.BigInteger(), nullable=True))
    op.create_index(op.f("ix_jobs_goal_id"), "jobs", ["goal_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_jobs_goal_id_goals"), "jobs", "goals", ["goal_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f("fk_jobs_goal_id_goals"), "jobs", type_="foreignkey")
    op.drop_index(op.f("ix_jobs_goal_id"), table_name="jobs")
    op.drop_column("jobs", "goal_id")
    op.drop_index(op.f("ix_goal_events_goal_id"), table_name="goal_events")
    op.drop_table("goal_events")
    op.drop_table("goal_steps")
    op.drop_index(op.f("ix_goals_status"), table_name="goals")
    op.drop_table("goals")
