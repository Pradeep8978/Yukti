"""
yukti/data/migrations/versions/006_journal_setup_outcome_fields.py
Add `setup_summary`, `outcome`, `reason`, and `is_high_conviction` columns to `journal_entries`.

Run: uv run alembic upgrade head
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "journal_entries",
        sa.Column("setup_summary", sa.Text(), nullable=True),
    )
    op.add_column(
        "journal_entries",
        sa.Column("outcome", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "journal_entries",
        sa.Column("reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "journal_entries",
        sa.Column("is_high_conviction", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("journal_entries", "is_high_conviction")
    op.drop_column("journal_entries", "reason")
    op.drop_column("journal_entries", "outcome")
    op.drop_column("journal_entries", "setup_summary")
