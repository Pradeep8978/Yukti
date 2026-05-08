YUKTI — Run Commands and Common Modes

# Setup (one-time)
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip setuptools wheel
# Install package (editable) if pyproject supports it, else install required deps
pip install -e .
# OR
# pip install -r requirements.txt

# Run (package entrypoint)
# Default (paper mode, safe):
python -m yukti

# Explicit modes:
python -m yukti --mode paper
python -m yukti --mode live
python -m yukti --mode shadow
python -m yukti --mode backtest --bt-start 2024-01-01 --bt-end 2024-12-31 --bt-sample 0.3

# Using the project's 'uv' wrapper (if available) — mirrors examples in repo:
uv run python -m yukti
uv run python -m yukti --mode live

# Run inside Docker (docker-compose)
# Build and start service (detached):
docker compose up -d --build yukti

# Rebuild & restart:
docker compose up -d --no-deps --build yukti

# Single command: rebuild, recreate, and start in LIVE mode (one-liner)
# Use `--no-deps` to avoid starting dependencies; remove if you want full stack
MODE=live docker compose up -d --no-deps --build --force-recreate yukti
# Full-stack alternative:
# MODE=live docker compose -f docker-compose.full.yml up -d --build --force-recreate

# Run one-off command inside the service container (no-deps):
docker compose run --rm yukti python -m yukti --mode paper

# Exec into running container and run manually:
CID=$(docker ps -q -f name=yukti | head -n1)
docker exec -it $CID bash
# then inside container:
PYTHONPATH=/app python -m yukti --mode paper

# Background (nohup/screen/systemd example)
nohup python -m yukti --mode paper &> yukti.log &
# For production, prefer systemd or container orchestration.

# Utilities / troubleshooting
# Check DhanHQ API auth using helper script (uses env vars by default):
PYTHONPATH=/app python3 scripts/check_dhan_api.py --client-id $DHAN_CLIENT_ID --access-token "$DHAN_ACCESS_TOKEN"

# Trigger renew-and-test job manually (inside container or local env):
# This runs the async job to renew the Dhan token, persist it, and call /profile.
PYTHONPATH=/app uv run python3 - <<'PY'
import asyncio
from yukti.scheduler.jobs import job_renew_and_test_dhan
asyncio.run(job_renew_and_test_dhan())
PY

# Run tests
pytest -q
pytest tests/unit -q

# Logs
# Tail service logs (docker):
docker logs --follow $(docker ps -q -f name=yukti)

# Environment tips
# Set the MODE env var to override default in .env:
export MODE=live
python -m yukti

# Notes:
# - Replace placeholders like $DHAN_ACCESS_TOKEN or $DHAN_CLIENT_ID with real values or set them in .env
# - Use the 'uv' wrapper if available in the container image; otherwise use the Python package entrypoint above.
# - For production, run via Docker Compose or orchestrator and manage secrets via environment or secret store.
