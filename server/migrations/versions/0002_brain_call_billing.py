"""brain calls record who pays: per-token usage or the user's plan

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-18 23:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "brain_calls",
        sa.Column("billing", sa.Text(), server_default="usage", nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("brain_calls", "billing")
