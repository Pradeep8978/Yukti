#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

echo "== Yukti: all-in-one local setup script =="

generate_hex() {
  # $1 = bytes
  openssl rand -hex "$1"
}

backup_env() {
  if [ -f .env ]; then
    cp .env .env.bak_$(date +%s)
    echo "Backed up existing .env to .env.bak_$(date +%s)"
  fi
}

ensure_env() {
  if [ ! -f .env ]; then
    echo "No .env found — copying .env.example → .env"
    cp .env.example .env
  else
    echo ".env already exists — will check required keys"
  fi

  # helper to set or replace a key only when missing/placeholder
  set_if_missing() {
    local key="$1"; local value="$2"
    if grep -q "^${key}=" .env; then
      cur=$(sed -n "s/^${key}=\(.*\)/\1/p" .env)
      if [ -z "$cur" ] || echo "$cur" | grep -qE "(<|your|REPLACE)"; then
        sed -i "s|^${key}=.*|${key}=${value}|" .env
        echo "Set ${key} in .env"
      else
        echo "${key} already set in .env (preserving)"
      fi
    else
      echo "${key}=${value}" >> .env
      echo "Added ${key} to .env"
    fi
  }

  POSTGRES_PASSWORD=$(generate_hex 16)
  CONTROL_API_KEY=$(generate_hex 32)

  set_if_missing "POSTGRES_PASSWORD" "$POSTGRES_PASSWORD"
  set_if_missing "CONTROL_API_KEY" "$CONTROL_API_KEY"
  # Ensure MODE=live for the default local run mode
  if grep -q '^MODE=' .env; then
    sed -i "s|^MODE=.*|MODE=live|" .env
  else
    echo "MODE=live" >> .env
  fi
  # Ensure POSTGRES_URL points to localhost for local runs
  if grep -q '^POSTGRES_URL=' .env; then
    sed -i "s|^POSTGRES_URL=.*|POSTGRES_URL=postgresql+psycopg://yukti:${POSTGRES_PASSWORD}@localhost:5432/yukti|" .env
  else
    echo "POSTGRES_URL=postgresql+psycopg://yukti:${POSTGRES_PASSWORD}@localhost:5432/yukti" >> .env
  fi

  echo "Prepared .env (first 80 lines):"
  sed -n '1,80p' .env || true
}

start_docker_services() {
  if command -v docker >/dev/null 2>&1; then
    echo "Docker detected — bringing up redis and postgres via docker compose"
    docker compose up -d redis postgres

    echo "Waiting for Postgres to be ready (timeout 120s) ..."
    SECONDS=0
    while [ $SECONDS -lt 120 ]; do
      # try pg_isready inside the postgres container
      if docker compose ps --status running | grep -q postgres; then
        if docker compose logs --no-color --tail 50 postgres 2>/dev/null | grep -q "database system is ready to accept connections"; then
          echo "Postgres ready"
          break
        fi
      fi
      sleep 3
    done
    if [ $SECONDS -ge 120 ]; then
      echo "Warning: Postgres may not be ready after 120s — continue and migrations may fail. Check containers with: docker compose ps && docker compose logs postgres"
    fi
  else
    echo "Docker not found — skipping container startup. Ensure Redis and Postgres are running for a full local run. (docker compose up -d redis postgres)"
  fi
}

create_venv_and_install() {
  if [ ! -d .venv ]; then
    if command -v python3 >/dev/null 2>&1; then
      echo "Creating venv at .venv"
      python3 -m venv .venv
    else
      echo "python3 not found. Please install Python 3.11+ and re-run this script." >&2
      exit 1
    fi
  else
    echo "Using existing .venv"
  fi

  # Activate and install
  # shellcheck source=/dev/null
  . .venv/bin/activate
  pip install -U pip setuptools wheel
  echo "Installing package (editable) and dependencies"
  pip install -e .
}

run_migrations() {
  echo "Running Alembic migrations"
  . .venv/bin/python -m alembic upgrade head
}

run_bootstrap() {
  echo "Running bootstrap script (may check Redis/Postgres)"
  . .venv/bin/python scripts/bootstrap.py || echo "bootstrap.py exited with non-zero status"
}

start_app_background() {
  MODE_VAL=$(grep -m1 '^MODE=' .env | sed 's/^MODE=//')
  MODE_VAL=${MODE_VAL:-paper}
  echo "Starting Yukti in background (mode=${MODE_VAL}) — logs -> yukti.log"
  nohup .venv/bin/python -m yukti --mode "${MODE_VAL}" > yukti.log 2>&1 &
  sleep 0.5
  PID=$!
  echo "Yukti started (pid=${PID}). Tail logs with: tail -n 200 -f yukti.log"
}


# --- main ---
backup_env
ensure_env
start_docker_services
create_venv_and_install
run_migrations
run_bootstrap
start_app_background

echo "All steps completed. If something failed, check the output above and logs: yukti.log"
