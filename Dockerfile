# ── Stage 1: Build React webapp ───────────────────────────────────────────────
FROM node:20-alpine AS webapp-build
WORKDIR /webapp
COPY webapp/package*.json ./
RUN npm ci --silent
COPY webapp/ .
RUN npm run build
# Output: /webapp/dist (vite.config.ts outDir set to yukti/api/static in build)

# ── Stage 2: Python trading agent ─────────────────────────────────────────────
FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl && rm -rf /var/lib/apt/lists/*
RUN curl -Ls https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.cargo/bin:$PATH"
COPY pyproject.toml .
RUN uv sync --frozen
COPY yukti/ ./yukti/
COPY scripts/ ./scripts/
# Inject built webapp into FastAPI static directory
COPY --from=webapp-build /webapp/dist ./yukti/api/static/
EXPOSE 8000
ENV MODE=paper
CMD ["uv", "run", "python", "-m", "yukti", "--mode", "paper"]
