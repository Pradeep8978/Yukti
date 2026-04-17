# Push Yukti to GitHub — Step-by-step guide

The code is ready to push. Here's how:

## Step 1: Create an empty GitHub repository

1. Go to https://github.com/new
2. Repository name: `yukti` (or `yukti-trading-agent`)
3. Description: "Autonomous NSE trading agent with Claude/Gemini AI reasoning"
4. **Visibility: PRIVATE** (recommended — contains trading logic)
5. Do NOT initialize with README, .gitignore, or LICENSE (we have them)
6. Click "Create repository"

GitHub will show you the push commands. Copy the HTTPS URL.

## Step 2: Add the remote and push

```bash
cd /home/claude/yukti

# Add GitHub as remote (replace YOUR_USERNAME and REPO_NAME)
git remote add origin https://github.com/YOUR_USERNAME/yukti.git

# Verify
git remote -v

# Push the initial commit
git branch -M main
git push -u origin main
```

Expected output:
```
Enumerating objects: 86, done.
Counting objects: 100% (86/86), done.
Delta compression using up to 8 threads
Compressing objects: 100%
...
 * [new branch]      main -> main
Branch 'main' set up to track remote branch 'main' from 'origin'.
```

## Step 3: Verify on GitHub

Go to https://github.com/YOUR_USERNAME/yukti — you should see:
- 86 files including README.md, pyproject.toml, Dockerfile
- Initial commit message with full architecture overview
- All subdirectories (yukti/, webapp/, scripts/, tests/, deploy/)

## Step 4: Set up for ongoing development

Create a `.github/workflows/` directory for CI/CD (optional but recommended):

```bash
mkdir -p .github/workflows
```

Create `.github/workflows/test.yml`:
```yaml
name: Tests

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - run: |
          pip install uv
          uv sync
      - run: uv run pytest tests/ -v
      - run: uv run ruff check yukti/
```

Then:
```bash
git add .github/workflows/test.yml
git commit -m "Add GitHub Actions CI workflow"
git push
```

## Step 5: Protect the main branch (optional but recommended)

On GitHub:
1. Settings → Branches
2. Add rule for branch `main`
3. Require pull request reviews before merging
4. Require status checks to pass before merging
5. Dismiss stale pull request approvals when new commits are pushed

This ensures no direct pushes to main — all changes go through PR review.

## Step 6: Configure branch protection secrets

For deployment to DigitalOcean or similar, you might want to add GitHub Secrets:

Settings → Secrets and variables → Actions

Add:
- `DHAN_CLIENT_ID` — for live testing
- `ANTHROPIC_API_KEY` — for CI tests
- `DOPPLER_TOKEN` — for production secrets (optional)

Then update `.github/workflows/test.yml` to use them:
```yaml
- run: uv run pytest tests/
  env:
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

## Ongoing workflow

After this point:

```bash
# Make changes
# Edit files...

# Stage and commit
git add .
git commit -m "feat: add shadow mode reconciliation"

# Push
git push origin main

# OR create a feature branch for PRs
git checkout -b feature/new-pattern-detector
# ... make changes ...
git push -u origin feature/new-pattern-detector
# Then create PR on GitHub web UI
```

## Next: Clone on your trading VM

Once the code is on GitHub, you can deploy to your DigitalOcean Bangalore VM:

```bash
# SSH into VM
ssh root@your-vm-ip

# Clone the repo
git clone https://github.com/YOUR_USERNAME/yukti.git
cd yukti

# Setup
cp .env.example .env
# Edit .env with real DhanHQ, Gemini, etc.

# Run
docker compose up -d
```

## Security note

✅ **NEVER commit to the repo:**
- `.env` files with real API keys
- `logs/` directory with real trades
- `backtest_trades.csv` with real P&L data

✅ **ALWAYS use:**
- `.env.example` (template with placeholders)
- GitHub Secrets for CI/CD
- Doppler for production secrets (Doppler has free tier)

---

**That's it!** Your code is now on GitHub and ready to collaborate, deploy, or share.

For any issues:
- GitHub auth: `git config --global credential.helper osxkeychain` (macOS) or use SSH key
- Already have a remote? `git remote rm origin` then add the new one
- Wrong URL? `git remote set-url origin https://github.com/...`
