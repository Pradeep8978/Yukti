# Yukti — Operational Runbook (Quick Playbook)

This runbook collects the common operational commands, canary promotion steps,
and recovery actions you will need when running Yukti in staging or production.

Prerequisites

- Environment: `.env` configured (or use Doppler)
- DB & Redis running and accessible
- `VOYAGE_API_KEY` set for embeddings

Bootstrap / Start

1. Start supporting services (docker-compose):

```bash
docker compose up -d redis postgres
```

2. Initialize the database (creates extensions and tables):

```bash
uv run python scripts/bootstrap.py
```

3. (Optional) Load the trading universe:

```bash
uv run python scripts/universe_loader.py --dynamic
```

4. Run Yukti in `paper` mode for validation:

```bash
uv run python -m yukti --mode paper
```

Indexing & RAG sanity

- Initialize LangChain vectorstore and index existing journals:

```bash
python scripts/index_journal_to_vectorstore.py
```

- Test retrieval quickly:

```bash
python scripts/test_rag_retriever.py "RELIANCE long pullback"
```

Canary promotion (happy path)

1. Produce a candidate artifact (trainer/exporter pipeline).
2. Run evaluation and generate metrics at `artifacts/eval/YYYYMMDD/compare_metrics.json`.
3. Run the CI gate locally to verify:

```bash
python scripts/check_promotion_gate.py --metrics artifacts/eval/YYYYMMDD/compare_metrics.json
```

4. Stage the canary via control-plane API (replace `path` and `ratio`):

```bash
curl -X POST "http://localhost:8000/control/canary/set" \
  -H "Content-Type: application/json" \
  -d '{"path":"models/canary/2026-04-25","ratio":0.10}'
```

5. Monitor for `canary_monitor_duration_seconds` (set in config) and watch metrics
(Prometheus + Grafana). If everything is healthy promote or increase ratio.

Quick rollback (manual)

```bash
curl -X POST "http://localhost:8000/control/canary/rollback"
```

Alertmanager / webhook test

Send a test alert payload to trigger webhook handling (Alertmanager-style):

```bash
curl -X POST "http://localhost:8000/control/alert" \
  -H "Content-Type: application/json" \
  -d '{"status":"firing","alerts":[{"labels":{"alertname":"YuktiCanaryFailure"},"annotations":{"summary":"canary failure test"}}]}'
```

Validate CI gating (locally)

```bash
python scripts/check_promotion_gate.py --metrics artifacts/eval/YYYYMMDD/compare_metrics.json
```

Emergency checklist (high level)

- If untrusted live orders are created, immediately call kill-switch:

```bash
curl -X POST "http://localhost:8000/control/halt"
```

- If Redis or DB are failing, stop new decision cycles by disabling scheduler
  or switching `MODE=shadow` until resolved.
- If canary degrades, use the rollback endpoint (above) and mark the artifact
  as failed in the registry.

Notes & references

- API control endpoints: [yukti/api/routes/positions.py](yukti/api/routes/positions.py)
- Artifact packaging: [yukti/artifacts.py](yukti/artifacts.py)
- Indexing script: [scripts/index_journal_to_vectorstore.py](scripts/index_journal_to_vectorstore.py)
- CI gate script: [scripts/check_promotion_gate.py](scripts/check_promotion_gate.py)

On-call tips

- Ensure Grafana alert rules are visible and Alertmanager is routing correctly.
- When troubleshooting decision quality, run `uv run python -m yukti.agents.quality --days 7`.

Recovery play (short)

1. Stop decision cycles (Mode=shadow or restart control plane paused).
2. Restore DB from latest backup if corruption detected.
3. Re-index embeddings using `scripts/index_journal_to_vectorstore.py`.
4. Re-run evaluation on candidate artifacts before re-promoting.
