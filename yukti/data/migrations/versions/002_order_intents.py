"""
yukti/data/migrations/versions/002_order_intents.py
Add order_intents table for crash-safe order state machine.

Run: uv run alembic upgrade head
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "order_intents",
        sa.Column("id",             sa.Integer,     primary_key=True, autoincrement=True),
        sa.Column("symbol",         sa.String(20),  nullable=False, index=True),
        sa.Column("security_id",    sa.String(20),  nullable=False),
        sa.Column("direction",      sa.String(5),   nullable=False),
        sa.Column("holding_period", sa.String(10),  nullable=False),
        sa.Column("quantity",       sa.Integer,     nullable=False),
        sa.Column("entry_price",    sa.Float,       nullable=False),
        sa.Column("stop_loss",      sa.Float,       nullable=False),
        sa.Column("target_1",       sa.Float,       nullable=False),
        sa.Column("target_2",       sa.Float,       nullable=True),
        sa.Column("conviction",     sa.Integer,     nullable=False),
        sa.Column("setup_type",     sa.String(30),  nullable=False),
        sa.Column("reasoning",      sa.Text,        nullable=False),

        sa.Column("state",          sa.String(15),  server_default="PLANNED", index=True),

        sa.Column("entry_order_id", sa.String(60),  nullable=True),
        sa.Column("sl_gtt_id",      sa.String(60),  nullable=True),
        sa.Column("target_gtt_id",  sa.String(60),  nullable=True),

        sa.Column("fill_price",     sa.Float,       nullable=True),
        sa.Column("filled_qty",     sa.Integer,     nullable=True),

        sa.Column("created_at",     sa.DateTime,    server_default=sa.func.now()),
        sa.Column("placed_at",      sa.DateTime,    nullable=True),
        sa.Column("filled_at",      sa.DateTime,    nullable=True),
        sa.Column("armed_at",       sa.DateTime,    nullable=True),
        sa.Column("closed_at",      sa.DateTime,    nullable=True),

        sa.Column("last_error",     sa.Text,        nullable=True),
    )

    # Index for the recovery scan: find_unsafe_intents() and find_stale_intents()
    op.create_index(
        "idx_intents_state_placed", "order_intents",
        ["state", "placed_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_intents_state_placed", "order_intents")
    op.drop_table("order_intents")
