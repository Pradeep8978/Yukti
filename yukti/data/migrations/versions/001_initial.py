"""
yukti/data/migrations/versions/001_initial.py
Initial schema migration — creates all Yukti tables.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── trades ────────────────────────────────────────────────────────────────
    op.create_table(
        "trades",
        sa.Column("id",              sa.Integer,     primary_key=True, autoincrement=True),
        sa.Column("symbol",          sa.String(20),  nullable=False,   index=True),
        sa.Column("security_id",     sa.String(20),  nullable=False),
        sa.Column("exchange",        sa.String(10),  server_default="NSE_EQ"),
        sa.Column("direction",       sa.String(5),   nullable=False),
        sa.Column("setup_type",      sa.String(30),  nullable=False),
        sa.Column("holding_period",  sa.String(10),  nullable=False),
        sa.Column("market_bias",     sa.String(10),  nullable=False),
        sa.Column("entry_price",     sa.Float,       nullable=False),
        sa.Column("stop_loss",       sa.Float,       nullable=False),
        sa.Column("target_1",        sa.Float,       nullable=False),
        sa.Column("target_2",        sa.Float,       nullable=True),
        sa.Column("quantity",        sa.Integer,     nullable=False),
        sa.Column("conviction",      sa.Integer,     nullable=False),
        sa.Column("risk_reward",     sa.Float,       nullable=False),
        sa.Column("max_loss",        sa.Float,       nullable=False),
        sa.Column("entry_order_id",  sa.String(60),  nullable=True),
        sa.Column("sl_gtt_id",       sa.String(60),  nullable=True),
        sa.Column("target_gtt_id",   sa.String(60),  nullable=True),
        sa.Column("status",          sa.String(15),  server_default="PENDING", index=True),
        sa.Column("exit_price",      sa.Float,       nullable=True),
        sa.Column("exit_reason",     sa.String(30),  nullable=True),
        sa.Column("pnl",             sa.Float,       nullable=True),
        sa.Column("pnl_pct",         sa.Float,       nullable=True),
        sa.Column("reasoning",       sa.Text,        nullable=False),
        sa.Column("opened_at",       sa.DateTime,    server_default=sa.func.now()),
        sa.Column("filled_at",       sa.DateTime,    nullable=True),
        sa.Column("closed_at",       sa.DateTime,    nullable=True),
    )

    # ── journal_entries ───────────────────────────────────────────────────────
    op.create_table(
        "journal_entries",
        sa.Column("id",         sa.Integer,  primary_key=True, autoincrement=True),
        sa.Column("trade_id",   sa.Integer,  sa.ForeignKey("trades.id"), index=True),
        sa.Column("symbol",     sa.String(20), nullable=False, index=True),
        sa.Column("setup_type", sa.String(30), nullable=False),
        sa.Column("direction",  sa.String(5),  nullable=False),
        sa.Column("pnl_pct",    sa.Float,      nullable=False),
        sa.Column("entry_text", sa.Text,       nullable=False),
        sa.Column("embedding",  Vector(1024),  nullable=True),
        sa.Column("created_at", sa.DateTime,   server_default=sa.func.now()),
    )
    op.create_index("idx_journal_embedding", "journal_entries",
                    ["embedding"], postgresql_using="ivfflat",
                    postgresql_with={"lists": "100"},
                    postgresql_ops={"embedding": "vector_cosine_ops"})

    # ── decision_log ──────────────────────────────────────────────────────────
    op.create_table(
        "decision_log",
        sa.Column("id",          sa.Integer,    primary_key=True, autoincrement=True),
        sa.Column("symbol",      sa.String(20), nullable=False, index=True),
        sa.Column("action",      sa.String(5),  nullable=False),
        sa.Column("direction",   sa.String(5),  nullable=True),
        sa.Column("market_bias", sa.String(10), nullable=True),
        sa.Column("conviction",  sa.Integer,    nullable=True),
        sa.Column("reasoning",   sa.Text,       nullable=False),
        sa.Column("skip_reason", sa.String(60), nullable=True),
        sa.Column("full_json",   sa.dialects.postgresql.JSONB, nullable=True),
        sa.Column("decided_at",  sa.DateTime,   server_default=sa.func.now(), index=True),
    )

    # ── daily_performance ─────────────────────────────────────────────────────
    op.create_table(
        "daily_performance",
        sa.Column("date",             sa.Date,   primary_key=True),
        sa.Column("trades_taken",     sa.Integer, server_default="0"),
        sa.Column("trades_won",       sa.Integer, server_default="0"),
        sa.Column("trades_lost",      sa.Integer, server_default="0"),
        sa.Column("gross_pnl",        sa.Float,   server_default="0"),
        sa.Column("max_drawdown_pct", sa.Float,   server_default="0"),
        sa.Column("win_rate",         sa.Float,   server_default="0"),
        sa.Column("profit_factor",    sa.Float,   server_default="0"),
    )

    # ── candles (TimescaleDB hypertable) ──────────────────────────────────────
    op.create_table(
        "candles",
        sa.Column("id",       sa.Integer,  primary_key=True, autoincrement=True),
        sa.Column("symbol",   sa.String(20), nullable=False, index=True),
        sa.Column("interval", sa.String(5),  nullable=False),
        sa.Column("time",     sa.DateTime,   nullable=False, index=True),
        sa.Column("open",     sa.Float,      nullable=False),
        sa.Column("high",     sa.Float,      nullable=False),
        sa.Column("low",      sa.Float,      nullable=False),
        sa.Column("close",    sa.Float,      nullable=False),
        sa.Column("volume",   sa.Float,      nullable=False),
    )
    # Convert to TimescaleDB hypertable (run manually after migration if needed)
    # op.execute("SELECT create_hypertable('candles', 'time', if_not_exists => TRUE)")


def downgrade() -> None:
    op.drop_table("candles")
    op.drop_table("daily_performance")
    op.drop_table("decision_log")
    op.drop_table("journal_entries")
    op.drop_table("trades")
