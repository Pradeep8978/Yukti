# Yukti — Architecture Overview

This document provides a concise architecture diagram and references to
the main implementation pieces for quick orientation and handoff.

```mermaid
flowchart LR
  Market[Market (NSE / BSE)] -->|Market data| Ingest[Ingestion & Feeds]
  Ingest --> Signals[Signals & Indicators]
  Signals --> PreFilter[Signal Pre-filter]
  PreFilter --> Arjun[AI Brain (`Arjun`) — yukti/agents/arjun.py]
  Arjun --> Risk[Risk Gates]
  Risk --> Execution[Execution (DhanHQ broker)]
  Execution --> Broker[DhanHQ]
  Execution --> Journal[Trade Journal (Postgres)]
  Journal --> Embedding[Embedding (Voyage AI)]
  Embedding --> VectorDB[Vector DB (pgvector)]
  VectorDB --> Arjun
  Arjun --> Web[Web portal (React) & API]
  Scheduler[Scheduler & Jobs] --> Signals
  Scheduler --> Learning[Learning Loop]
  Learning --> Artifacts[Artifacts / Model Registry]
  Artifacts --> CI[CI Gate (GitHub Actions)]
  CI --> Canary[Canary Promotion]
  Canary --> Arjun
  Ops[Ops / Alertmanager] -->|Alert| Control[Control plane API]
  Control --> Rollback[Rollback / Promote]
```

Key components and where to find them

- **AI brain (`Arjun`)** — provider-agnostic decision engine.
  - Implementation: [yukti/agents/arjun.py](yukti/agents/arjun.py)
- **Local adapters & RAG** — local PEFT adapters, LangChain retriever and vectorstore.
  - Retriever: [yukti/agents/langchain_rag.py](yukti/agents/langchain_rag.py)
  - VectorStore adapter: [yukti/agents/langchain_vectorstore.py](yukti/agents/langchain_vectorstore.py)
  - Memory/embed helpers: [yukti/agents/memory.py](yukti/agents/memory.py)
- **Scheduler & Jobs** — all recurring jobs and the self-learning loop.
  - Jobs: [yukti/scheduler/jobs.py](yukti/scheduler/jobs.py)
- **Execution & Order State Machine**
  - Order intents and broker plumbing: [yukti/execution/order_intent.py](yukti/execution/order_intent.py)
- **Persistence & Schema** — Postgres, TimescaleDB and pgvector models.
  - ORM models: [yukti/data/models.py](yukti/data/models.py)
  - DB engine: [yukti/data/database.py](yukti/data/database.py)
- **Control plane / API** — operational endpoints (canary, alert webhook, control actions).
  - API routes: [yukti/api/routes/positions.py](yukti/api/routes/positions.py)
- **Artifacts & Registry** — packaging, checksum, and optional S3 upload/signing.
  - Artifact tooling: [yukti/artifacts.py](yukti/artifacts.py)
- **CI Gate** — promotion gating scripts & workflows
  - Gate script: [scripts/check_promotion_gate.py](scripts/check_promotion_gate.py)
  - Workflow: [.github/workflows/gate-promotion.yml](.github/workflows/gate-promotion.yml)

Data flows (short)

- Market data is ingested and pre-filtered by `Signals` to reduce API/compute
  cost. Signals produce candidate contexts for the `Arjun` brain.
- `Arjun` (AI) returns a deterministic JSON `TradeDecision` which is validated
  and passed through deterministic risk gates before any order is placed.
- Executed trades write a `JournalEntry` which is embedded (Voyage AI) and
  persisted into Postgres `pgvector`. The learning loop indexes these into
  the LangChain-compatible vectorstore for future retrieval.

Operational considerations

- Keep the `artifact_registry_signing_key` in a secret manager (Doppler/KMS).
- Prefer managed Postgres with pgvector support for production; tune the
  ivfflat index lists value after observing dataset size.
- Keep a rolling backup schedule and WAL archiving; instrument RTO/RPO in runbook.

Want a printable runbook and quick operational playbook? See: [docs/RUNBOOK.md](docs/RUNBOOK.md)
