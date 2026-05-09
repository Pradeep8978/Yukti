"""
yukti/data/migrations/versions/005_journal_reflection_fields.py
Add structured reflection fields to `journal_entries` table.

Run: uv run alembic upgrade head
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "journal_entries",
        sa.Column("quality_score", sa.Float(), nullable=True),
    )
    op.add_column(
        "journal_entries",
        sa.Column("key_lesson", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "journal_entries",
        sa.Column("market_regime", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "journal_entries",
        sa.Column("outcome_reason", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "journal_entries",
        sa.Column("one_actionable_lesson", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "journal_entries",
        sa.Column(
            "discarded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("journal_entries", "discarded")
    op.drop_column("journal_entries", "one_actionable_lesson")
    op.drop_column("journal_entries", "outcome_reason")
    op.drop_column("journal_entries", "market_regime")
    op.drop_column("journal_entries", "key_lesson")
    op.drop_column("journal_entries", "quality_score")
