#!/usr/bin/env bash
set -euo pipefail

# run-local.sh — convenience script to run Yukti locally without Docker
# Usage: ./run-local.sh [command]
# Commands:
#   setup     — create venv, install deps, and prepare .env
#   migrate   — run alembic migrations
#   bootstrap  — run scripts/bootstrap.py (requires Redis + Postgres running)
#   start [mode] — start Yukti in given mode (default: live)

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"

usage() {
  echo "Usage: $0 {setup|migrate|bootstrap|start} [mode]"
  exit 2
}

ensure_venv() {
  if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
  fi
  # shellcheck source=/dev/null
  source "$VENV_DIR/bin/activate"
}

case ${1-} in
  setup)
    ensure_venv
    pip install -U pip setuptools wheel
    pip install -e .
    if [ ! -f .env ]; then
      cp .env.example .env
      echo "Copied .env.example to .env — please edit .env and set POSTGRES_URL, REDIS_URL, and any API keys."
    else
      echo ".env already exists — edit it if needed."
    fi
    ;;

  migrate)
    ensure_venv
    echo "Running Alembic migrations..."
    python -m alembic upgrade head
    ;;

  bootstrap)
    ensure_venv
    echo "Running bootstrap (will check Redis + Postgres connections)..."
    python scripts/bootstrap.py
    ;;

  start)
    MODE=${2-live}
    ensure_venv
    export MODE
    echo "Starting Yukti in mode=$MODE"
    python -m yukti --mode "$MODE"
    ;;

  *)
    usage
    ;;
esac
