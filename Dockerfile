# Serves the API and the built app. Build data first (uv run synthpop fetch && uv run synthpop build)
# so data/out/ exists; only data/out/ and config/ are copied into the image.
#
# The image is built for whichever CPU you ask for. For ECS on Graviton (ARM64, the default on an
# Apple Silicon Mac): docker build --platform linux/arm64 . ; for x86 hosts: --platform linux/amd64.

# The front end is plain JS and CSS, so build it on the build machine's own CPU.
FROM --platform=$BUILDPLATFORM node:24-slim AS app
WORKDIR /app
COPY app/package.json app/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY app/ ./
RUN npm run build

FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.14 /uv /usr/local/bin/uv
WORKDIR /srv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 PATH="/srv/.venv/bin:$PATH"
COPY pyproject.toml uv.lock README.md ./
COPY synthpop/ synthpop/
RUN uv sync --frozen --no-dev --extra aws
COPY config/ config/
COPY data/out/ data/out/
COPY --from=app /app/dist app/dist
EXPOSE 8000
# Settings, all optional: SYNTHPOP_PASSWORD (turns on basic auth), SYNTHPOP_USER (default "synthpop"),
# SYNTHPOP_DATAPOINTS (where saved data points live: a path or s3://bucket/key).
CMD ["synthpop", "serve", "--host", "0.0.0.0", "--port", "8000"]
