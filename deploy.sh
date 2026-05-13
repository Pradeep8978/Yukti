#!/usr/bin/env bash
set -euo pipefail

# One-shot deploy script for Yukti on an Ubuntu VPS (Docker)
# Usage: sudo ./deploy.sh [env-file]
# If no env-file passed, uses .env in repo root (created from .env.example if missing).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ENV_FILE="${1:-.env}"
IMAGE_NAME="${IMAGE_NAME:-yukti:latest}"
CONTAINER_NAME="${CONTAINER_NAME:-yukti}"
HOST_PORT="${HOST_PORT:-8000}"
APP_PORT="${APP_PORT:-8000}"
MODE="${MODE:-live}"

echo "[deploy] repo: $SCRIPT_DIR"
echo "[deploy] env file: $ENV_FILE"

if ! command -v docker >/dev/null 2>&1; then
  echo "[deploy] docker not found — installing (requires sudo)..."
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl gnupg lsb-release
  curl -fsSL https://get.docker.com -o get-docker.sh
  sudo sh get-docker.sh
  rm -f get-docker.sh
fi

if ! sudo systemctl is-active --quiet docker 2>/dev/null; then
  echo "[deploy] starting docker service"
  sudo systemctl enable --now docker || true
fi

mkdir -p models artifacts logs

if [ ! -f "$ENV_FILE" ]; then
  if [ -f .env.example ]; then
    cp .env.example .env
    echo "[deploy] created .env from .env.example — edit with credentials as needed"
  else
    cat > .env <<EOF
# Minimal .env for Yukti
MODE=live
ENABLE_SELF_LEARNING=false
ENABLE_CANARY_ROUTING=false
CANARY_RATIO=0.1
POSTGRES_URL=postgresql+psycopg://yukti:password@localhost:5432/yukti
REDIS_URL=redis://localhost:6379/0
# TELEGRAM_BOT_TOKEN=
# TELEGRAM_CHAT_ID=
EOF
    echo "[deploy] created a minimal .env — edit it before enabling live features"
  fi
fi

echo "[deploy] building Docker image ${IMAGE_NAME}..."
docker build -t "$IMAGE_NAME" .

if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
  echo "[deploy] stopping and removing existing container ${CONTAINER_NAME}..."
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
fi

echo "[deploy] running container ${CONTAINER_NAME} (host ${HOST_PORT} -> container ${APP_PORT})"
docker run -d \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  -p "${HOST_PORT}:${APP_PORT}" \
  -v "${SCRIPT_DIR}/models:/app/models" \
  -v "${SCRIPT_DIR}/artifacts:/app/artifacts" \
  -v "${SCRIPT_DIR}/${ENV_FILE}:/app/.env:ro" \
  --env-file "${SCRIPT_DIR}/${ENV_FILE}" \
  -e MODE="$MODE" \
  "$IMAGE_NAME"

echo "[deploy] container started — to follow logs: docker logs -f ${CONTAINER_NAME}"
echo "[deploy] stop: docker stop ${CONTAINER_NAME} && docker rm ${CONTAINER_NAME}"
echo "[deploy] If you changed docker group membership, re-login to use docker without sudo."
