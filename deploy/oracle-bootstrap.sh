#!/usr/bin/env bash
# oracle-bootstrap.sh
# Run this ONCE on a fresh Oracle Cloud Always Free (Ubuntu 22.04, ARM64) instance.
# Usage:  bash oracle-bootstrap.sh
# After it completes, push to main and the GitHub Actions deploy workflow will handle all future deploys.

set -euo pipefail
DEPLOY_DIR="${DEPLOY_DIR:-$HOME/yukti}"
GITHUB_REPO="${GITHUB_REPO:-}"   # e.g. myorg/yukti  (set via env or prompted below)

echo "=== Yukti — Oracle Cloud bootstrap ==="

# ── 1. System packages ────────────────────────────────────────────────────────
echo "[1/7] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg git ufw

# ── 2. Docker (official repo, ARM64-compatible) ───────────────────────────────
echo "[2/7] Installing Docker..."
if ! command -v docker &>/dev/null; then
    curl -fsSL https://get.docker.com | sudo sh
fi
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
echo "  Docker $(docker --version) installed."

# ── 3. Firewall — open only what is needed ────────────────────────────────────
echo "[3/7] Configuring UFW firewall..."
sudo ufw --force reset
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp    comment 'SSH'
sudo ufw allow 8000/tcp  comment 'Yukti API + webapp'
# Uncomment if you want Prometheus/Grafana reachable from outside:
# sudo ufw allow 9090/tcp  comment 'Prometheus'
# sudo ufw allow 3000/tcp  comment 'Grafana'
sudo ufw --force enable
echo "  UFW status:"
sudo ufw status numbered

# ── 4. Oracle Cloud VCN note ──────────────────────────────────────────────────
echo ""
echo "  *** ACTION REQUIRED (one-time, in Oracle Cloud Console) ***"
echo "  Add Ingress rules to your instance's Security List / NSG:"
echo "    TCP  port 22   (SSH)"
echo "    TCP  port 8000 (Yukti app)"
echo "  Without this the app will be unreachable even after UFW allows it."
echo ""

# ── 5. Clone repo ─────────────────────────────────────────────────────────────
echo "[4/7] Cloning repository..."
if [ -z "$GITHUB_REPO" ]; then
    read -rp "  Enter GitHub repo (e.g. myorg/yukti): " GITHUB_REPO
fi
if [ ! -d "$DEPLOY_DIR/.git" ]; then
    git clone "https://github.com/${GITHUB_REPO}.git" "$DEPLOY_DIR"
else
    echo "  Repo already cloned at $DEPLOY_DIR — skipping."
fi

# ── 6. Create .env ────────────────────────────────────────────────────────────
echo "[5/7] Setting up .env..."
if [ ! -f "$DEPLOY_DIR/.env" ]; then
    if [ -f "$DEPLOY_DIR/.env.example" ]; then
        cp "$DEPLOY_DIR/.env.example" "$DEPLOY_DIR/.env"
        echo "  Copied .env.example → .env"
    else
        cat > "$DEPLOY_DIR/.env" <<'EOF'
# ── Yukti runtime environment ──────────────────────────────────────────────
MODE=paper                          # paper | shadow | live

# Postgres (used by docker-compose; keep in sync with POSTGRES_URL below)
POSTGRES_PASSWORD=change_me_now

# Connection URLs (these match the docker-compose service names)
POSTGRES_URL=postgresql+psycopg://yukti:change_me_now@postgres:5432/yukti
REDIS_URL=redis://redis:6379/0

# Broker (Dhan) — only needed for live/shadow mode
# DHAN_CLIENT_ID=
# DHAN_ACCESS_TOKEN=

# OpenAI (for Arjun LLM reasoning) — optional, falls back to rule-based
# OPENAI_API_KEY=

# Telegram alerts — optional
# TELEGRAM_BOT_TOKEN=
# TELEGRAM_CHAT_ID=

# Grafana
GRAFANA_PASSWORD=change_me_now

# Feature flags
ENABLE_SELF_LEARNING=false
ENABLE_CANARY_ROUTING=false
CANARY_RATIO=0.1
EOF
        echo "  Created .env — EDIT IT NOW before running the app:"
        echo "    nano $DEPLOY_DIR/.env"
    fi
else
    echo "  .env already exists — skipping."
fi

# ── 7. GHCR login so docker compose can pull the image ───────────────────────
echo "[6/7] GHCR authentication..."
echo ""
echo "  The deploy workflow pushes the image to ghcr.io."
echo "  This server needs a GitHub PAT (read:packages scope) to pull it."
echo ""
read -rp "  Enter GitHub username: " GH_USER
read -rsp "  Enter GitHub PAT (read:packages): " GH_PAT
echo ""
echo "$GH_PAT" | docker login ghcr.io -u "$GH_USER" --password-stdin
echo "  GHCR login saved to ~/.docker/config.json"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "[7/7] Bootstrap complete!"
echo ""
echo "Next steps:"
echo "  1. Edit $DEPLOY_DIR/.env with real credentials."
echo "  2. Add these secrets to your GitHub repo"
echo "     (Settings → Secrets and variables → Actions):"
echo ""
echo "     VPS_HOST        = $(curl -s ifconfig.me 2>/dev/null || echo '<this server public IP>')"
echo "     VPS_USER        = $USER"
echo "     VPS_SSH_KEY     = <contents of your SSH private key>"
echo "     VPS_DEPLOY_DIR  = $DEPLOY_DIR"
echo "     GHCR_TOKEN      = <same PAT you just entered>"
echo "     POSTGRES_PASSWORD = <value from .env>"
echo "     GRAFANA_PASSWORD  = <value from .env>"
echo ""
echo "  3. Push to main — the CD workflow will build, push, and deploy automatically."
echo ""
echo "  TIP: Log out and back in (or run 'newgrp docker') so the docker group applies."
