# Serves the API and the built app. Build data first (uv run synthpop fetch && uv run synthpop build)
# so data/out/ exists; only data/out/ and config/ are copied into the image.
FROM node:24-slim AS app
WORKDIR /app
COPY app/package.json app/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY app/ ./
RUN npm run build

FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.14 /uv /usr/local/bin/uv
WORKDIR /srv
COPY pyproject.toml uv.lock README.md ./
COPY synthpop/ synthpop/
RUN uv sync --frozen --no-dev
COPY config/ config/
COPY data/out/ data/out/
COPY --from=app /app/dist app/dist
EXPOSE 8000
CMD ["uv", "run", "--no-dev", "synthpop", "serve", "--host", "0.0.0.0", "--port", "8000"]
