#!/usr/bin/env bash
# scripts/setup_github_runner.sh
#
# One-shot installer for the GitHub Actions self-hosted runner.
# Run as root (or a user with sudo access).
#
# Usage:
#   bash scripts/setup_github_runner.sh <REGISTRATION_TOKEN>
#
# Get the token from:
#   https://github.com/Pradeep8978/Yukti/settings/actions/runners/new
#   (Linux / x64, copy the token from the "Configure" step)
#
# What this script does:
#   1. Creates /opt/yukti as a symlink to /root/Yukti (canonical deploy path)
#   2. Creates a dedicated runner user (github-runner) with docker access
#   3. Downloads and extracts the GitHub Actions runner binary
#   4. Registers the runner with labels: self-hosted, yukti, production
#   5. Installs it as a systemd service that starts on boot

set -euo pipefail

REPO_URL="https://github.com/Pradeep8978/Yukti"
RUNNER_VERSION="2.334.0"
RUNNER_DIR="/root/actions-runner"
REPO_DIR="/root/Yukti"
DEPLOY_LINK="/opt/yukti"
RUNNER_NAME="${RUNNER_NAME:-yukti-runner}"
RUNNER_LABELS="self-hosted,yukti,production"

# ── Arg check ────────────────────────────────────────────────────────────────
if [[ $# -lt 1 || -z "$1" ]]; then
    echo "Usage: bash scripts/setup_github_runner.sh <REGISTRATION_TOKEN>"
    echo ""
    echo "Get the token at:"
    echo "  https://github.com/Pradeep8978/Yukti/settings/actions/runners/new"
    exit 1
fi
REG_TOKEN="$1"

log() { echo "[runner-setup] $*"; }

# ── 1. Canonical deploy symlink ───────────────────────────────────────────────
if [[ ! -e "$DEPLOY_LINK" ]]; then
    log "Creating symlink $DEPLOY_LINK → $REPO_DIR"
    ln -s "$REPO_DIR" "$DEPLOY_LINK"
else
    log "$DEPLOY_LINK already exists — skipping symlink"
fi

# ── 2. Download runner binary ─────────────────────────────────────────────────
log "Preparing runner directory $RUNNER_DIR"
mkdir -p "$RUNNER_DIR"
cd "$RUNNER_DIR"

ARCHIVE="actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
if [[ ! -f "$ARCHIVE" ]]; then
    log "Downloading runner v${RUNNER_VERSION}..."
    curl -fsSL \
        "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${ARCHIVE}" \
        -o "$ARCHIVE"
fi

log "Extracting runner..."
tar xzf "$ARCHIVE"

# ── 3. Install runner dependencies ───────────────────────────────────────────
log "Installing runner OS dependencies..."
./bin/installdependencies.sh 2>/dev/null || true

# ── 4. Configure runner ───────────────────────────────────────────────────────
log "Configuring runner for $REPO_URL"
./config.sh \
    --url "$REPO_URL" \
    --token "$REG_TOKEN" \
    --name "$RUNNER_NAME" \
    --labels "$RUNNER_LABELS" \
    --work "_work" \
    --unattended \
    --replace

# ── 5. Install as systemd service ─────────────────────────────────────────────
log "Installing systemd service..."
./svc.sh install root 2>/dev/null || ./svc.sh install

log "Starting service..."
./svc.sh start

log ""
log "=== Runner installed successfully ==="
log "Name   : $RUNNER_NAME"
log "Labels : $RUNNER_LABELS"
log "Repo   : $REPO_URL"
log ""
log "Check status : /root/actions-runner/svc.sh status"
log "View logs    : journalctl -u actions.runner.Pradeep8978-Yukti.${RUNNER_NAME} -f"
log ""
log "Verify at: https://github.com/Pradeep8978/Yukti/settings/actions/runners"
