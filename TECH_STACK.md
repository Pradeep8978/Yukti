# TECH_STACK.md

This document maps the key technologies used in this repository to the broader course-style tech stack (agent frameworks, LLM providers, RAG, training, infra). It also includes quick-start commands and recommendations for contributors.

---

## Quick summary
- Primary language: Python 3.11+
- App framework: FastAPI (HTTP), APScheduler for jobs
- Agents: custom `Arjun` provider abstraction (`yukti.agents.arjun`) with A/B testing, canary routing, and local adapter support
- LLM Providers: Anthropic (`claude`) and Google Gemini (`google-genai`) supported; OpenAI is not included by default but easy to add
- Embeddings/vector store: `voyageai` embeddings + Postgres `pgvector` (Timescale optional)
- Model training: `transformers`, `peft` (LoRA), `bitsandbytes`, `torch` used in `trainer/` scripts (optional heavy deps)
- Observability: `prometheus-client` metrics, `docker-compose.yml` includes Prometheus & Grafana stubs
- Deployment: Dockerfile + `deploy.sh` for easy VPS deploy; Docker Compose for local stacks

---

## Mapping to course stack

- Agent frameworks
  - Course examples: OpenAI Agents SDK, CrewAI, LangGraph, AutoGen
  - This repo: custom agent (`yukti.agents.arjun`, `ABTestProvider`, `CanaryRouterProvider`)
  - Notes: functional equivalent for production usage; adapters can be added to wrap LangChain or OpenAI Agents if desired

- LLM Providers
  - Course examples: OpenAI, Anthropic
  - This repo: Anthropic (`anthropic`), Google Gemini (`google-genai`) via `arjun.py`. Add `openai` to `pyproject.toml` and a small provider module to support OpenAI.

- RAG / Vector DB
  - Course: Chroma/Chromadb, LangChain
  - This repo: `voyageai` embeddings + `pgvector` backed by Postgres; found in `data/` and `docker-compose.yml` (pgvector init)

- Training + Adapters
  - Course: `transformers`, `peft` (LoRA), `bitsandbytes`, `torch`
  - This repo: `trainer/` folder with `train_adapter.py`, `evaluate_vs_baseline.py`, `prototype_finetune.py`, and `trainer/requirements.txt` listing `transformers`, `peft`, `bitsandbytes`, `torch`.

- Observability & Infra
  - Course: Prometheus, Grafana, Docker, CI
  - This repo: `yukti/metrics.py`, `docker-compose.yml` includes Prometheus & Grafana; `Dockerfile` and `deploy.sh` provided; CI gate script `scripts/check_promotion_gate.py` exists.

---

## Key files & locations
- App entry: `yukti/__main__.py` (exposes `yukti` CLI)
- Agent brain: `yukti/agents/arjun.py`
- Local adapter: `yukti/agents/local_adapter.py`
- Canary manager: `yukti/agents/canary.py`
- Scheduler jobs: `yukti/scheduler/jobs.py` (includes self-learning loop)
- Metrics: `yukti/metrics.py`
- Trainer scripts: `trainer/` (see `trainer/requirements.txt`)
- Artifact packaging/uploader: `yukti/artifacts.py`
- Deploy helper: `deploy.sh`

---

## Dependencies (high level)
- Runtime (in `pyproject.toml`): pydantic, pydantic-settings, httpx, tenacity, structlog, anthropic, google-genai, voyageai, pandas, numpy, pandas-ta, dhanhq, redis[hiredis], sqlalchemy, alembic, psycopg[binary], pgvector, fastapi, uvicorn[standard], aiofiles, python-multipart, apscheduler, python-telegram-bot, prometheus-client
- Trainer extras (heavy): `transformers`, `peft`, `bitsandbytes`, `torch` (listed in `trainer/requirements.txt`)
- Dev extras: pytest, pytest-asyncio, ruff, mypy (in pyproject optional deps)

---

## Quick start (development)
1. Create a venv and install runtime deps (or use `uv`):

```bash
python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install .
```

2. Configure `.env` (example keys: `POSTGRES_URL`, `REDIS_URL`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`)

3. Run in paper mode locally:

```bash
MODE=live uv run python -m yukti --mode live
# or: python -m yukti --mode paper
```

4. Build & run in Docker (recommended for VPS):

```bash
chmod +x deploy.sh
sudo ./deploy.sh .env
docker logs -f yukti
```

5. Trainer (optional; only on GPU or large enough host):

```bash
pip install -r trainer/requirements.txt
python trainer/train_adapter.py --data data/training/journal_export.jsonl --model facebook/opt-125m --out_dir models/lora-journal --use_peft auto
```

---

## How to add course-parity integrations

- Add OpenAI provider:
  - `pip install openai`
  - Add small provider module `yukti/agents/openai_provider.py` mirroring `arjun` provider structure and register in `arjun.build_arjun()`.

- Add LangChain RAG adapter that uses `pgvector`:
  - Install `langchain` and `chromadb` if desired
  - Implement a thin wrapper that maps `voyageai`/pgvector queries to LangChain retriever API; keep `pgvector` as the store to avoid data migration.

---

## Production recommendations
- Keep `ENABLE_SELF_LEARNING=false` in staging until canary flows are validated.
- Use managed Postgres/Redis; do not colocate DB+app on small VPS.
- Push artifacts to an S3-compatible store via `yukti/artifacts.py`.
- Monitor with Prometheus/Grafana and set alerts for canary regressions.

---

If you'd like, I can now:
- add an `openai` provider module and env configuration, or
- add a LangChain RAG adapter (pgvector) with examples.
