# Self-Hosted GitHub Actions Runner

This repo supports an optional self-hosted deploy workflow:

```text
.github/workflows/deploy-self-hosted.yml
```

Use this when the GitHub Actions runner is installed directly on the Yukti VM. In that setup, GitHub does not SSH into the server. The runner receives the job and deploys locally from `/opt/yukti`.

## Why Use This

Benefits:

- No `VPS_HOST`, `VPS_USER`, or `VPS_SSH_KEY` secrets needed.
- No GHCR pull token needed if the VM builds the image locally.
- Deployment is simpler: `git fetch`, `git reset`, `docker compose up -d --build`.

Tradeoffs:

- The VM now runs GitHub job commands, so protect repository write access.
- The runner user must have Docker access.
- If the VM is down, deploy jobs wait or fail.

## 1. Add Runner in GitHub

Open:

```text
Repository -> Settings -> Actions -> Runners -> New self-hosted runner
```

Choose:

```text
Linux
x64
```

GitHub will show commands similar to these. Run them on the VM:

```bash
mkdir -p ~/actions-runner
cd ~/actions-runner
curl -o actions-runner-linux-x64.tar.gz -L https://github.com/actions/runner/releases/download/<version>/actions-runner-linux-x64-<version>.tar.gz
tar xzf ./actions-runner-linux-x64.tar.gz
./config.sh --url https://github.com/pradeeprlck/Yukti --token <github_runner_registration_token>
```

When prompted for labels, use:

```text
self-hosted,yukti,production
```

The workflow uses these labels:

```yaml
runs-on: [self-hosted, yukti, production]
```

## 2. Install Runner as a Service

From the runner directory:

```bash
sudo ./svc.sh install
sudo ./svc.sh start
sudo ./svc.sh status
```

## 3. Give Runner Docker Access

Find the service user. If you installed the runner as the current user, it is usually `azureuser`.

Add that user to the Docker group:

```bash
sudo usermod -aG docker azureuser
```

Restart the runner service:

```bash
cd ~/actions-runner
sudo ./svc.sh stop
sudo ./svc.sh start
```

Verify Docker works as the runner user:

```bash
docker compose version
```

## 4. Ensure `/opt/yukti` Is Ready

The self-hosted workflow deploys from `/opt/yukti`, so the runner user needs access:

```bash
sudo chown -R azureuser:azureuser /opt/yukti
cd /opt/yukti
git remote -v
docker compose ps
```

The server must have:

```text
/opt/yukti/.env
```

Do not commit `.env` to GitHub.

## 5. Required GitHub Secrets

For the self-hosted workflow, you do not need these SSH/GHCR deploy secrets:

```text
VPS_HOST
VPS_USER
VPS_SSH_KEY
VPS_DEPLOY_DIR
GHCR_TOKEN
```

Keep any application secrets in `/opt/yukti/.env` on the VM.

If you keep using the original `CD - Build & Deploy` workflow, that workflow still needs the SSH/GHCR secrets.

## 6. Deploy

Open:

```text
GitHub -> Actions -> Self-Hosted Deploy -> Run workflow
```

Choose:

```text
paper
```

After it finishes, check on the VM:

```bash
cd /opt/yukti
docker compose ps
curl http://localhost:8000/health
```

## 7. Avoid Double Deploys

There are now two deploy paths:

```text
CD - Build & Deploy       # GHCR + SSH deploy
Self-Hosted Deploy        # local VM runner deploy
```

Use only one production deploy path at a time.

The self-hosted workflow is manual-only by default, so it will not automatically deploy on every push unless you add a `workflow_run` or `push` trigger later.
