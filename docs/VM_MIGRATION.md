# VM Migration Runbook

Use this runbook when moving Yukti from one VM to another.

The migration has four parts:

1. Prepare the new VM.
2. Copy the application config and database data.
3. Start the Docker Compose stack on the new VM.
4. Update GitHub Actions secrets so future deploys target the new VM.

> Safety note: do not run `MODE=live` on both VMs at the same time. Stop the old app before promoting the new VM to live mode.

## 1. Prepare the New VM

Install Git and Docker:

```bash
sudo apt update
sudo apt install -y git curl
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
```

Log out and back in so the Docker group change takes effect, then verify:

```bash
docker compose version
```

Create the deployment directory and clone the repository:

```bash
sudo mkdir -p /opt/yukti
sudo chown "$USER:$USER" /opt/yukti
git clone https://github.com/pradeeprlck/Yukti.git /opt/yukti
cd /opt/yukti
```

## 2. Copy Secrets

Copy the runtime environment file from the old VM to the new VM.

From your local machine:

```bash
scp azureuser@OLD_VM_PUBLIC_IP:/opt/yukti/.env ./yukti.env
scp ./yukti.env NEW_VM_USER@NEW_VM_PUBLIC_IP:/opt/yukti/.env
```

Do not commit `.env` to GitHub.

## 3. Migrate PostgreSQL Data

Create a database dump on the old VM:

```bash
ssh azureuser@OLD_VM_PUBLIC_IP
cd /opt/yukti
docker exec yukti-postgres-1 pg_dump -U yukti -d yukti > yukti.sql
exit
```

Copy the dump to the new VM:

```bash
scp azureuser@OLD_VM_PUBLIC_IP:/opt/yukti/yukti.sql ./yukti.sql
scp ./yukti.sql NEW_VM_USER@NEW_VM_PUBLIC_IP:/opt/yukti/yukti.sql
```

Start PostgreSQL and Redis on the new VM:

```bash
ssh NEW_VM_USER@NEW_VM_PUBLIC_IP
cd /opt/yukti
docker compose up -d postgres redis
```

Restore the database:

```bash
cat yukti.sql | docker exec -i yukti-postgres-1 psql -U yukti -d yukti
```

## 4. Start Yukti on the New VM

Start the full stack:

```bash
cd /opt/yukti
docker compose up -d
docker compose ps
```

Check app health:

```bash
curl http://localhost:8000/health
```

Useful service URLs:

```text
App:        http://NEW_VM_PUBLIC_IP:8000
Grafana:    http://NEW_VM_PUBLIC_IP:3000
Prometheus: http://NEW_VM_PUBLIC_IP:9090
```

## 5. Update GitHub Actions Secrets

In GitHub, open:

```text
Repository -> Settings -> Secrets and variables -> Actions
```

Update these secrets:

```text
VPS_HOST=NEW_VM_PUBLIC_IP
VPS_USER=NEW_VM_USER
VPS_DEPLOY_DIR=/opt/yukti
VPS_SSH_KEY=<private SSH key that can log in to the new VM>
```

Keep or update these depending on the new VM config:

```text
POSTGRES_PASSWORD=<value from /opt/yukti/.env>
GRAFANA_PASSWORD=<optional Grafana admin password>
GHCR_TOKEN=<GitHub PAT with read:packages>
```

After updating the secrets, run the deploy workflow manually:

```text
GitHub -> Actions -> CD - Build & Deploy -> Run workflow
```

## 6. DNS and HTTPS

If a domain points to the old VM, update its DNS `A` record to the new VM public IP.

If HTTPS is terminated on the VM, issue fresh certificates after DNS points to the new VM. Reusing certificates from the old VM is possible, but issuing fresh certs is usually cleaner.

## 7. Stop the Old VM

After the new VM is healthy, stop the old Yukti app:

```bash
ssh azureuser@OLD_VM_PUBLIC_IP
cd /opt/yukti
docker compose stop yukti
```

Once you are confident the new VM is stable, stop the rest of the old stack or shut down the VM.

## Current Server Values

As of the current server setup:

```text
Current VPS_HOST=20.40.40.246
Current VPS_USER=azureuser
Current VPS_DEPLOY_DIR=/opt/yukti
```

Replace these with the new VM values during migration.
