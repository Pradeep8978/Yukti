"""
yukti/data/migrations/versions/003_positions.py
Add `positions` table for authoritative open positions.

Run: uv run alembic upgrade head
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "positions",
        sa.Column("id",            sa.Integer,     primary_key=True, autoincrement=True),
        sa.Column("symbol",        sa.String(20),  nullable=False, index=True),
        sa.Column("security_id",   sa.String(20),  nullable=False),
        sa.Column("intent_id",     sa.Integer,     sa.ForeignKey("order_intents.id"), nullable=True),

        sa.Column("direction",     sa.String(5),   nullable=False),
        sa.Column("setup_type",    sa.String(30),  nullable=False),
        sa.Column("holding_period",sa.String(10),  nullable=False),

        sa.Column("entry_price",   sa.Float,       nullable=False),
        sa.Column("fill_price",    sa.Float,       nullable=True),
        sa.Column("stop_loss",     sa.Float,       nullable=False),
        sa.Column("target_1",      sa.Float,       nullable=False),
        sa.Column("target_2",      sa.Float,       nullable=True),

        sa.Column("quantity",      sa.Integer,     nullable=False),
        sa.Column("conviction",    sa.Integer,     nullable=False),
        sa.Column("risk_reward",   sa.Float,       nullable=False),

        sa.Column("entry_order_id",sa.String(60),  nullable=True),
        sa.Column("sl_gtt_id",     sa.String(60),  nullable=True),
        sa.Column("target_gtt_id", sa.String(60),  nullable=True),

        sa.Column("status",        sa.String(15),  server_default="OPEN", index=True),
        sa.Column("reasoning",     sa.Text,        nullable=True),

        sa.Column("opened_at",     sa.DateTime,    server_default=sa.func.now()),
        sa.Column("filled_at",     sa.DateTime,    nullable=True),
    )


def downgrade() -> None:
    op.drop_table("positions")
