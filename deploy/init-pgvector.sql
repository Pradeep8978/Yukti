-- deploy/init-pgvector.sql
-- Run once at first container start.
-- Enables the vector and timescaledb extensions in the yukti database.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Convert candles table to a TimescaleDB hypertable after Alembic creates it.
-- Called from scripts/bootstrap.py after migrations run.
-- SELECT create_hypertable('candles', 'time', if_not_exists => TRUE);
