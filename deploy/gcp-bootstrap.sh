#!/usr/bin/env bash
# gcp-bootstrap.sh
# Run this ONCE on a fresh GCP Compute Engine VM (Ubuntu 22.04, e2-small).
# Usage:  bash gcp-bootstrap.sh
#
# Recommended VM spec (fits within $300 trial credit for ~5 months):
#   Machine type : e2-small  (2 vCPU, 2 GB RAM)  ~$15/month
#   Region       : asia-south1-a (Mumbai — lowest NSE latency)
#   Disk         : 20 GB balanced persistent disk
#   Image        : Ubuntu 22.04 LTS
#
# After it completes, push to main and the GitHub Actions deploy workflow handles future deploys.

set -euo pipefail
DEPLOY_DIR="${DEPLOY_DIR:-$HOME/yukti}"
GITHUB_REPO="${GITHUB_REPO:-}"   # e.g. myorg/yukti

echo "=== Yukti — GCP Compute Engine bootstrap ==="

# ── 1. System packages ────────────────────────────────────────────────────────
echo "[1/7] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg git ufw cron

# ── 2. Docker (official repo) ────────────────────────────────────────────────
echo "[2/7] Installing Docker..."
if ! command -v docker &>/dev/null; then
    curl -fsSL https://get.docker.com | sudo sh
fi
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
echo "  Docker $(docker --version) installed."

# ── 3. Firewall — GCP uses VPC firewall rules, but UFW as defense-in-depth ──
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

# ── 4. GCP VPC firewall note ─────────────────────────────────────────────────
echo ""
echo "  *** ACTION REQUIRED (one-time, in GCP Console) ***"
echo "  Create a VPC firewall rule to allow ingress on port 8000:"
echo ""
echo "    gcloud compute firewall-rules create yukti-web \\"
echo "      --allow tcp:8000 \\"
echo "      --target-tags yukti \\"
echo "      --source-ranges 0.0.0.0/0 \\"
echo "      --description 'Allow Yukti web portal'"
echo ""
echo "  Then tag your VM with 'yukti':"
echo "    gcloud compute instances add-tags YOUR_VM_NAME --tags yukti --zone asia-south1-a"
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
# ── Yukti runtime environment (GCP) ───────────────────────────────────────
MODE=live                           # paper | shadow | live

# Postgres (used by docker-compose; keep in sync with POSTGRES_URL below)
POSTGRES_PASSWORD=change_me_now

# Connection URLs (these match the docker-compose service names)
POSTGRES_URL=postgresql+psycopg://yukti:change_me_now@postgres:5432/yukti
REDIS_URL=redis://redis:6379/0

# AI providers — at least one required
# ANTHROPIC_API_KEY=sk-ant-xxx
# GEMINI_API_KEY=your_gemini_key

# Embeddings
# VOYAGE_API_KEY=your_voyage_key

# Broker (Dhan) — only needed for live/shadow mode
# DHAN_CLIENT_ID=
# DHAN_ACCESS_TOKEN=

# Telegram alerts — optional
# TELEGRAM_BOT_TOKEN=
# TELEGRAM_CHAT_ID=

# Grafana
GRAFANA_PASSWORD=change_me_now

# Feature flags
ENABLE_SELF_LEARNING=false
ENABLE_CANARY_ROUTING=false
CANARY_RATIO=0.1

# Watchlist (comma-separated NSE symbols)
WATCHLIST=RELIANCE,HDFCBANK,INFY,TCS,ICICIBANK
ACCOUNT_VALUE=500000
EOF
        echo "  Created .env — EDIT IT NOW:"
        echo "    nano $DEPLOY_DIR/.env"
    fi
else
    echo "  .env already exists — skipping."
fi

# ── 7. GHCR login ────────────────────────────────────────────────────────────
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

# ── 8. Swap file (important for e2-small with 2GB RAM) ───────────────────────
echo "[7/7] Configuring swap..."
if [ ! -f /swapfile ]; then
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
    echo "  2 GB swap enabled."
else
    echo "  Swap already configured — skipping."
fi

# ── 9. Host cron for Dhan token renewal ─────────────────────────────────────
echo "[8/8] Installing Dhan token renewal cron..."
sudo systemctl enable --now cron
mkdir -p "$DEPLOY_DIR/logs"
RENEW_CMD="bash $DEPLOY_DIR/scripts/renew_dhan_token.sh >> $DEPLOY_DIR/logs/renew.log 2>&1"
(
    crontab -l 2>/dev/null | grep -v 'renew_dhan_token.sh' || true
    echo "30 2 * * * $RENEW_CMD"
    echo "30 12 * * * $RENEW_CMD"
) | crontab -
echo "  Installed cron entries for 08:00 IST and 18:00 IST daily."

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "=== Bootstrap complete! ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env:              nano $DEPLOY_DIR/.env"
echo "  2. Start the stack:        cd $DEPLOY_DIR && docker compose up -d"
echo "  3. Watch logs:             docker compose logs -f yukti"
echo "  4. Open web portal:        http://$(curl -s ifconfig.me):8000"
echo "  5. Token renewal cron:     08:00 IST and 18:00 IST daily"
echo ""
echo "GCP budget tip: Set a budget alert at \$50/month in"
echo "  Console → Billing → Budgets & alerts"
echo ""
echo "To create the VM via CLI (for reference):"
echo "  gcloud compute instances create yukti \\"
echo "    --machine-type e2-small \\"
echo "    --zone asia-south1-a \\"
echo "    --image-family ubuntu-2204-lts \\"
echo "    --image-project ubuntu-os-cloud \\"
echo "    --boot-disk-size 20GB \\"
echo "    --tags yukti"
