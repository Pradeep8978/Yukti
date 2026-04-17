"""
scripts/secrets_loader.py
Optional Doppler integration. Use instead of .env in production.

Setup (one-time):
    1. Sign up: https://doppler.com (free tier)
    2. Install CLI: brew install dopplerhq/cli/doppler  (or curl install)
    3. Login: doppler login
    4. Setup project: doppler setup --project yukti --config prd
    5. Add all secrets: doppler secrets set DHAN_CLIENT_ID=xxx GEMINI_API_KEY=xxx ...

Runtime:
    # Wrap the agent with doppler run
    doppler run -- uv run python -m yukti --mode paper

    # Or this script fetches into env before the agent starts
    python scripts/secrets_loader.py && uv run python -m yukti

Benefits vs .env file:
    - No secrets on disk ever
    - Automatic rotation: change in Doppler UI, agent picks up next restart
    - Audit log of who accessed what secret when
    - Separate configs for dev/staging/prod
    - Free tier covers this use case
"""
from __future__ import annotations

import json
import os
import subprocess
import sys


REQUIRED_SECRETS = [
    "DHAN_CLIENT_ID",
    "DHAN_ACCESS_TOKEN",
    "GEMINI_API_KEY",        # or ANTHROPIC_API_KEY
    "VOYAGE_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "POSTGRES_PASSWORD",
]


def load_from_doppler() -> dict[str, str]:
    """Fetch all secrets from Doppler as a dict."""
    try:
        result = subprocess.run(
            ["doppler", "secrets", "download", "--no-file", "--format", "json"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            print(f"[error] doppler CLI failed: {result.stderr}", file=sys.stderr)
            return {}
        return json.loads(result.stdout)
    except FileNotFoundError:
        print("[error] Doppler CLI not installed", file=sys.stderr)
        return {}
    except subprocess.TimeoutExpired:
        print("[error] Doppler CLI timeout", file=sys.stderr)
        return {}


def validate_required(secrets: dict[str, str]) -> list[str]:
    """Return list of missing required secrets."""
    # GEMINI_API_KEY or ANTHROPIC_API_KEY — at least one required
    ai_ok = bool(secrets.get("GEMINI_API_KEY") or secrets.get("ANTHROPIC_API_KEY"))

    missing = []
    for key in REQUIRED_SECRETS:
        if key in ("GEMINI_API_KEY",):
            continue  # handled separately
        if not secrets.get(key):
            missing.append(key)

    if not ai_ok:
        missing.append("GEMINI_API_KEY or ANTHROPIC_API_KEY")

    return missing


def main() -> int:
    print("Loading secrets from Doppler...")
    secrets = load_from_doppler()

    if not secrets:
        print("[fatal] No secrets loaded", file=sys.stderr)
        return 1

    missing = validate_required(secrets)
    if missing:
        print(f"[fatal] Missing required secrets: {missing}", file=sys.stderr)
        return 1

    # Write to env file for the agent process (or use doppler run directly)
    print(f"[ok] Loaded {len(secrets)} secrets")
    print("Run: doppler run -- uv run python -m yukti")
    return 0


if __name__ == "__main__":
    sys.exit(main())
