# ── Stage 1: Build React webapp ───────────────────────────────────────────────
FROM node:20-alpine AS webapp-build
WORKDIR /webapp
COPY webapp/package*.json ./
RUN npm ci --silent
COPY webapp/ .
RUN npm run build
# Output: /webapp/dist (vite.config.ts outDir set to yukti/api/static in build)

# ── Stage 2: Python trading agent ─────────────────────────────────────────────
FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl && rm -rf /var/lib/apt/lists/*
RUN curl -Ls https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"
ENV UV_PYTHON=python3.12
COPY pyproject.toml .
COPY uv.lock .
COPY README.md .
COPY yukti/ ./yukti/
COPY scripts/ ./scripts/
# Curated 50-symbol watchlist — static fallback if Redis is wiped on startup.
COPY universe.json .
RUN uv sync --frozen
# Inject built webapp into FastAPI static directory
COPY --from=webapp-build /webapp/dist ./yukti/api/static/
EXPOSE 8000
ENV MODE=live
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uv", "run", "python", "-m", "yukti"]
