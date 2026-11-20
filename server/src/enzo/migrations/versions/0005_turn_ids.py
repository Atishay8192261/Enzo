"""brain and tool calls carry the id of the run that made them, the same as its log lines

Also repairs #43: a failed or stopped call on the ChatGPT plan was recorded as per-token usage of
unknown cost, which then showed "cost unknown" for the whole chat. Such a call made by a brain
that the same chat used on the plan is the plan's.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-20 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    for table in ("brain_calls", "tool_calls"):
        op.add_column(table, sa.Column("turn_id", sa.Text(), nullable=True))
        op.create_index(op.f(f"ix_{table}_turn_id"), table, ["turn_id"])
    op.execute(
        """
        UPDATE brain_calls AS failed SET billing = 'plan'
        WHERE failed.outcome <> 'ok' AND failed.billing = 'usage' AND failed.cost_usd IS NULL
          AND EXISTS (
            SELECT 1 FROM brain_calls AS paid
            WHERE paid.conversation_id = failed.conversation_id
              AND paid.brain = failed.brain AND paid.billing = 'plan'
          )
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    for table in ("tool_calls", "brain_calls"):
        op.drop_index(op.f(f"ix_{table}_turn_id"), table_name=table)
        op.drop_column(table, "turn_id")
