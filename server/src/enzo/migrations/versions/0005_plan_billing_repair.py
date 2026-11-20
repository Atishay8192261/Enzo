"""a failed or stopped call on the ChatGPT plan is the plan's

Repairs #43: such a call was recorded as per-token usage of unknown cost, which then showed "cost
unknown" for the whole chat. A failed call made by a brain that the same chat used on the plan is
the plan's.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-20 14:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
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
    """Downgrade schema: the repair is kept (it only corrects who paid)."""
