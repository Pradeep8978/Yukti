# Yukti Web Portal

React 18 + TypeScript + Vite + Tailwind CSS dashboard for the Yukti trading agent.

## Stack

| Tool           | Purpose                                          |
|----------------|--------------------------------------------------|
| React 18       | UI framework                                     |
| TypeScript     | Type safety across the codebase                  |
| Vite           | Build tool and dev server (with FastAPI proxy)   |
| Tailwind CSS   | Utility-first styling                            |
| Recharts       | P&L equity charts, performance charts            |
| date-fns       | Date formatting                                  |
| React Router   | Client-side routing (SPA)                        |

## Pages

| Route        | Purpose                                             |
|--------------|-----------------------------------------------------|
| `/`          | Dashboard — P&L stats, equity chart, live positions |
| `/positions` | Detailed open position cards with level bars        |
| `/trades`    | Full trade history table                            |
| `/journal`   | Arjun's post-trade reflections (written by Claude)  |
| `/control`   | Kill switch, halt/resume, emergency squareoff       |

## Setup

```bash
cd webapp
npm install          # or: pnpm install / bun install
```

## Development

The dev server proxies `/api/*` and `/ws/*` to FastAPI at `localhost:8000`.
Make sure FastAPI is running first:

```bash
# Terminal 1 — start FastAPI
cd ..
uv run python -m yukti --mode paper

# Terminal 2 — start Vite dev server
cd webapp
npm run dev
# → http://localhost:5173
```

## Production build

```bash
cd webapp
npm run build
# Outputs to: yukti/api/static/
# FastAPI automatically serves this at /
```

After building, visiting `http://localhost:8000` serves the portal directly from FastAPI.
No separate web server needed.

## Docker (auto-build)

The Dockerfile has a multi-stage build that compiles the webapp before starting the agent:

```dockerfile
# Stage 1 — build webapp
FROM node:20-alpine AS webapp-build
WORKDIR /webapp
COPY webapp/package*.json ./
RUN npm ci
COPY webapp/ .
RUN npm run build

# Stage 2 — Python agent
FROM python:3.11-slim
COPY --from=webapp-build /webapp/dist /app/yukti/api/static
# ... rest of Dockerfile
```

## Live updates via WebSocket

The portal connects to `ws://<host>/ws/live` on load. FastAPI pushes a JSON state
update every 5 seconds:

```json
{
  "type":      "state_update",
  "halted":    false,
  "perf":      { "daily_pnl_pct": 1.2, "win_rate_last_10": 0.6, ... },
  "positions": { "RELIANCE": { ... } },
  "timestamp": "2025-01-15T10:30:00"
}
```

The portal also sends control messages back over the same socket:

```json
{ "type": "halt"   }   // activate kill switch
{ "type": "resume" }   // resume trading
{ "type": "ping"   }   // keep-alive
```

## Environment variables

Create `webapp/.env.local` to override defaults:

```env
VITE_API_BASE=http://localhost:8000   # default: /api (proxied)
VITE_WS_URL=ws://localhost:8000/ws/live
```
