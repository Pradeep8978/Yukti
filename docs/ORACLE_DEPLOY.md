# Deploying Yukti on Oracle Cloud Always Free

This guide walks through deploying Yukti on an Oracle Cloud Always Free ARM64 instance at zero cost, with automatic deploys triggered by GitHub Actions on every push to `main`.

---

## Architecture overview

```
Push to main
    │
    ▼
GitHub Actions (tests.yml)
    │  passes
    ▼
GitHub Actions (deploy.yml)
    ├─ Builds multi-arch Docker image (AMD64 + ARM64)
    ├─ Pushes to GitHub Container Registry (ghcr.io) — free
    └─ SSHes into Oracle instance → pulls image → restarts container
                                        │
                              Oracle A1 Free Instance (ARM64)
                              ├─ yukti  (bot + FastAPI)  :8000
                              ├─ postgres + pgvector      :5432
                              ├─ redis                    :6379
                              ├─ prometheus               :9090
                              └─ grafana                  :3000
```

---

## Part 1 — Oracle Cloud account & instance

### 1.1 Create an account

1. Go to **https://cloud.oracle.com** and click **Start for free**.
2. Fill in your details. A credit/debit card is required for identity verification — **nothing is charged**.
3. When asked to choose a home region, select **US East (Ashburn)** (`us-ashburn-1`). This region has the most Always Free A1 capacity.

> The Always Free A1 quota is **3,000 OCPU-hours and 18,000 GB-hours per month** — enough for one VM running 4 OCPUs and 24 GB RAM continuously (4 × 720 h = 2,880 OCPU-hours).

---

### 1.2 Create the compute instance

1. In the Oracle Console, go to **Compute → Instances → Create Instance**.
2. Set the following:

   | Field | Value |
   |---|---|
   | Name | `yukti` |
   | Image | Ubuntu 22.04 (Canonical) |
   | Shape | `VM.Standard.A1.Flex` |
   | OCPUs | 4 |
   | Memory | 24 GB |
   | Boot volume | 50 GB (default) |

3. Under **Add SSH keys**, choose **Generate a key pair for me** and download both files (`ssh-key-*.key` private, `ssh-key-*.key.pub` public). Keep the private key safe.
4. Click **Create**.
5. Wait ~2 minutes for the instance to reach **Running** state. Note the **Public IP address**.

---

### 1.3 Open ports in the VCN Security List

The instance is behind Oracle's Virtual Cloud Network (VCN) — ports must be opened there in addition to the OS firewall.

1. In the instance detail page, click the **Subnet** link → **Security List** → **Add Ingress Rules**.
2. Add these two rules:

   | Source CIDR | Protocol | Port | Description |
   |---|---|---|---|
   | `0.0.0.0/0` | TCP | 22 | SSH |
   | `0.0.0.0/0` | TCP | 8000 | Yukti app |

   > Optional — add port 9090 (Prometheus) and 3000 (Grafana) if you want external access to metrics dashboards. Leave them closed for now and use SSH tunnelling instead.

---

## Part 2 — Bootstrap the server (run once)

### 2.1 SSH into the instance

```bash
chmod 400 ssh-key-*.key
ssh -i ssh-key-*.key ubuntu@<oracle-public-ip>
```

### 2.2 Run the bootstrap script

The script installs Docker, configures the firewall, clones your repo, creates `.env`, and authenticates with GHCR.

```bash
# Option A — pipe directly from GitHub (replace with your repo)
GITHUB_REPO=<your-github-username>/yukti \
  bash <(curl -fsSL https://raw.githubusercontent.com/<your-github-username>/yukti/main/deploy/oracle-bootstrap.sh)

# Option B — clone first, then run
git clone https://github.com/<your-github-username>/yukti ~/yukti
bash ~/yukti/deploy/oracle-bootstrap.sh
```

The script will prompt for:
- Your GitHub username
- A GitHub Personal Access Token with **`read:packages`** scope (to pull the Docker image from GHCR)

To create the PAT:  
GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic) → New token → tick **`read:packages`** → Generate.

### 2.3 Edit `.env`

```bash
nano ~/yukti/.env
```

Fill in at minimum:

```env
MODE=paper                    # start with paper to verify everything works
POSTGRES_PASSWORD=<strong-password>
POSTGRES_URL=postgresql+psycopg://yukti:<strong-password>@postgres:5432/yukti
REDIS_URL=redis://redis:6379/0

# AI provider (pick one)
GEMINI_API_KEY=<your-key>     # free tier: 15 rpm
# or
ANTHROPIC_API_KEY=<your-key>

# Voyage AI (journal embeddings)
VOYAGE_API_KEY=<your-key>

# Broker — leave blank for paper mode
DHAN_CLIENT_ID=
DHAN_ACCESS_TOKEN=

# Telegram alerts — optional
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Save with `Ctrl+O`, exit with `Ctrl+X`.

### 2.4 Log out and back in

```bash
exit
ssh -i ssh-key-*.key ubuntu@<oracle-public-ip>
```

This applies the `docker` group so you can run Docker without `sudo`.

---

## Part 3 — GitHub repository secrets

Go to your GitHub repo → **Settings → Secrets and variables → Actions → New repository secret** and add each of the following:

| Secret name | Value |
|---|---|
| `VPS_HOST` | Oracle instance public IP address |
| `VPS_USER` | `ubuntu` |
| `VPS_SSH_KEY` | Full contents of your downloaded `.key` private key file |
| `VPS_DEPLOY_DIR` | `/home/ubuntu/yukti` |
| `GHCR_TOKEN` | The same PAT you entered during bootstrap (`read:packages`) |
| `POSTGRES_PASSWORD` | The password you put in `.env` |
| `GRAFANA_PASSWORD` | Grafana admin password (optional, defaults to `admin`) |

---

## Part 4 — First deploy

Push any commit to `main`:

```bash
git add .
git commit -m "chore: trigger first deploy"
git push origin main
```

GitHub Actions will:
1. Run unit tests (`test.yml`)
2. Build a multi-arch Docker image (AMD64 + ARM64) and push to `ghcr.io`
3. SSH into the Oracle instance, pull the new image, and restart the `yukti` container

Watch progress at: `https://github.com/<you>/yukti/actions`

The first build takes ~8 minutes (QEMU ARM64 emulation). Subsequent builds are faster due to layer caching.

---

## Part 5 — Verify it's working

```bash
# App health check
curl http://<oracle-public-ip>:8000/health

# Container status
ssh -i ssh-key-*.key ubuntu@<oracle-public-ip>
cd ~/yukti
docker compose ps

# Logs
docker compose logs -f yukti
```

Open the web dashboard: **`http://<oracle-public-ip>:8000`**

---

## Part 6 — Switching to live trading

When you're satisfied paper mode is working:

1. Edit `.env` on the server:
   ```bash
   nano ~/yukti/.env
   # Set: MODE=live
   # Set: DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN
   ```

2. Trigger a redeploy via GitHub Actions:
   - Go to Actions → **CD — Build & Deploy** → **Run workflow** → set `mode` to `live` → Run.

   Or restart the container directly:
   ```bash
   cd ~/yukti
   export MODE=live
   docker compose up -d --no-deps --force-recreate yukti
   ```

---

## Ongoing operations

### View logs
```bash
docker compose logs -f yukti          # bot logs
docker compose logs -f postgres       # DB logs
```

### Restart a service
```bash
docker compose restart yukti
```

### Update manually (without a push)
```bash
cd ~/yukti
git pull
docker compose pull yukti
docker compose up -d --no-deps --force-recreate yukti
```

### Prometheus + Grafana (metrics)
Access via SSH tunnel to avoid opening extra ports:
```bash
ssh -i ssh-key-*.key -L 9090:localhost:9090 -L 3000:localhost:3000 ubuntu@<oracle-public-ip>
```
Then open:
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000 (login: `admin` / your `GRAFANA_PASSWORD`)

### Run database migrations
```bash
cd ~/yukti
docker compose exec yukti uv run alembic upgrade head
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| SSH connection refused | Check Oracle VCN Security List has port 22 open |
| App unreachable on :8000 | Check VCN Security List has port 8000 open; check `docker compose ps` shows yukti as `Up` |
| `permission denied` on docker commands | Log out and back in to apply the `docker` group |
| Deploy workflow fails at SSH step | Verify `VPS_SSH_KEY` secret contains the full key including `-----BEGIN...` and `-----END...` lines |
| ARM64 image missing / wrong arch | Ensure `deploy.yml` has `platforms: linux/amd64,linux/arm64` in the build step |
| Out of disk space | Run `docker image prune -f` and `docker system prune -f` |
| A1 shape unavailable during signup | Try again in a few hours — free capacity is region-limited; `us-ashburn-1` has the most |
