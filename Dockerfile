# rmapi (maintained fork) - talks to the reMarkable cloud
FROM golang:1.24-bookworm AS rmapi
RUN git clone --depth 1 https://github.com/ddvk/rmapi /src \
 && cd /src && CGO_ENABLED=0 go build -trimpath -o /rmapi .

# livesync-bridge - headless Self-hosted LiveSync client, run by rmbridge
FROM denoland/deno:2.9.7 AS livesync
ARG LIVESYNC_BRIDGE_REF=fc0e028d33b3a12ddee1322951375e05107edc55
ENV DENO_DIR=/opt/deno
USER root
# Deno keeps localStorage (the bridge's sync checkpoints) in $DENO_DIR/location_data;
# it's symlinked onto the persistent /data volume.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
 && git clone https://github.com/vrtmrz/livesync-bridge /opt/livesync-bridge \
 && cd /opt/livesync-bridge && git checkout "$LIVESYNC_BRIDGE_REF" && rm -rf .git \
 && deno install --frozen \
 && deno cache main.ts \
 && rm -rf /opt/deno/location_data && ln -s /data/state/livesync/location_data /opt/deno/location_data \
 && cp "$(command -v deno)" /deno

FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends pandoc \
 && rm -rf /var/lib/apt/lists/*
COPY --from=rmapi /rmapi /usr/local/bin/rmapi
COPY --from=livesync /deno /usr/local/bin/deno
COPY --from=livesync /opt/livesync-bridge /opt/livesync-bridge
COPY --from=livesync /opt/deno /opt/deno
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY rmbridge ./rmbridge
# deno writes small analysis caches next to the module cache at runtime
RUN uv sync --frozen --no-dev && chmod -R a+rX /app /opt && chmod -R a+w /opt/deno

ENV PATH=/app/.venv/bin:$PATH \
    NAS_ROOT=/nas \
    DATA_DIR=/data/state \
    DENO_DIR=/opt/deno \
    LIVESYNC_BRIDGE_DIR=/opt/livesync-bridge \
    RMAPI_CONFIG=/data/state/rmapi.conf \
    XDG_CACHE_HOME=/data/state/cache \
    HOME=/data/state
EXPOSE 8484
CMD ["rmbridge"]
