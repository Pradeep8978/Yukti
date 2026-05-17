"""
yukti/data/migrations/versions/007_position_extended_fields.py
Add trailing_sl, atr_trail_distance, and slippage_pct columns to positions.

Run: uv run alembic upgrade head
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("positions", sa.Column("trailing_sl", sa.Float(), nullable=True))
    op.add_column("positions", sa.Column("atr_trail_distance", sa.Float(), nullable=True))
    op.add_column("positions", sa.Column("slippage_pct", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("positions", "slippage_pct")
    op.drop_column("positions", "atr_trail_distance")
    op.drop_column("positions", "trailing_sl")
