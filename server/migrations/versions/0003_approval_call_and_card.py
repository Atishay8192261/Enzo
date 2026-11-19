"""approvals remember their tool call and what the card says, so a reopened app shows it

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-19 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("approvals", sa.Column("call_id", sa.Text(), nullable=True))
    op.add_column("approvals", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column("approvals", sa.Column("detail", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("approvals", "detail")
    op.drop_column("approvals", "summary")
    op.drop_column("approvals", "call_id")
