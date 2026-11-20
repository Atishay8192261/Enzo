"""chats can be moved to the trash, and chats saved before titles existed get one

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-20 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "conversations", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True)
    )
    # Older chats: the first line of the first message, as new chats get it (#38).
    op.execute(
        """
        UPDATE conversations AS c
        SET title = left(btrim(split_part(m.body -> 'parts' -> 0 ->> 'text', E'\\n', 1)), 60)
        FROM (
            SELECT DISTINCT ON (conversation_id) conversation_id, body
            FROM messages WHERE role = 'user' ORDER BY conversation_id, seq
        ) AS m
        WHERE m.conversation_id = c.id AND c.title IS NULL
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("conversations", "archived_at")
