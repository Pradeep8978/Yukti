"""
yukti/data/migrations/005_enhanced_journal_fields.py
Add enhanced fields to journal_entries for hybrid retrieval and quality scoring.

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
    # Add new structured fields for hybrid retrieval
    op.add_column(
        "journal_entries",
        sa.Column("setup_summary", sa.Text(), nullable=True),
    )
    op.add_column(
        "journal_entries",
        sa.Column("outcome", sa.String(20), nullable=True),  # WIN | LOSS | BREAKEVEN
    )
    op.add_column(
        "journal_entries",
        sa.Column("reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "journal_entries",
        sa.Column("one_actionable_lesson", sa.Text(), nullable=True),
    )
    op.add_column(
        "journal_entries",
        sa.Column("quality_score", sa.Float(), nullable=True),  # 0-10 self-score
    )
    op.add_column(
        "journal_entries",
        sa.Column("market_regime", sa.String(30), nullable=True),  # BULLISH | BEARISH | NEUTRAL | VOLATILE
    )
    op.add_column(
        "journal_entries",
        sa.Column("is_high_conviction", sa.Boolean(), nullable=False, server_default="false"),
    )

    # Add indexes for hybrid retrieval filters
    op.create_index(
        "journal_entries_outcome_idx",
        "journal_entries",
        ["outcome"],
        if_not_exists=True,
    )
    op.create_index(
        "journal_entries_symbol_idx",
        "journal_entries",
        ["symbol"],
        if_not_exists=True,
    )
    op.create_index(
        "journal_entries_created_at_idx",
        "journal_entries",
        ["created_at"],
        if_not_exists=True,
    )
    op.create_index(
        "journal_entries_quality_idx",
        "journal_entries",
        ["quality_score"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("journal_entries_quality_idx", table_name="journal_entries")
    op.drop_index("journal_entries_created_at_idx", table_name="journal_entries")
    op.drop_index("journal_entries_symbol_idx", table_name="journal_entries")
    op.drop_index("journal_entries_outcome_idx", table_name="journal_entries")

    op.drop_column("journal_entries", "is_high_conviction")
    op.drop_column("journal_entries", "market_regime")
    op.drop_column("journal_entries", "quality_score")
    op.drop_column("journal_entries", "one_actionable_lesson")
    op.drop_column("journal_entries", "reason")
    op.drop_column("journal_entries", "outcome")
    op.drop_column("journal_entries", "setup_summary")