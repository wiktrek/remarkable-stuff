# rmapi (maintained fork) - talks to the reMarkable cloud
FROM golang:1.24-bookworm AS rmapi
RUN git clone --depth 1 https://github.com/ddvk/rmapi /src \
 && cd /src && CGO_ENABLED=0 go build -trimpath -o /rmapi .

FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends pandoc \
 && rm -rf /var/lib/apt/lists/*
COPY --from=rmapi /rmapi /usr/local/bin/rmapi
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY rmbridge ./rmbridge
RUN uv sync --frozen --no-dev && chmod -R a+rX /app

ENV PATH=/app/.venv/bin:$PATH \
    NAS_ROOT=/nas \
    DATA_DIR=/data/state \
    RMAPI_CONFIG=/data/state/rmapi.conf \
    XDG_CACHE_HOME=/data/state/cache \
    HOME=/data/state
EXPOSE 8484
CMD ["rmbridge"]
