# Base images pinned by exact tag AND digest; bump both together:
#   docker buildx imagetools inspect <image>:<tag>
FROM ghcr.io/astral-sh/uv:0.11.14@sha256:1025398289b62de8269e70c45b91ffa37c373f38118d7da036fb8bb8efc85d97 AS uv

FROM python:3.12.14-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
# dependencies from the lockfile first (cached), then the project itself
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md PROTOCOL.md LICENSE ./
COPY docs ./docs
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12.14-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
RUN groupadd --system --gid 10001 channel \
 && useradd --system --uid 10001 --gid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin channel \
 && mkdir -p /data && chown 10001:10001 /data && chmod 700 /data
COPY --from=build /opt/venv /opt/venv
COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh

# channel mailboxes + admin.db live here — mount a volume over it
ENV AI_AGENT_CHANNEL_DATA_DIR=/data \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

EXPOSE 8765
# Liveness from inside the container, with the interpreter the image already
# has (slim images carry no curl). /healthz is answered before the Host
# allowlist and needs no token, so this works whatever
# AI_AGENT_CHANNEL_ALLOWED_HOSTS says. Any failure raises, i.e. exits non-zero.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4).status == 200 else 1)"]
# The entrypoint starts as root only to fix ownership of a pre-existing
# root-owned volume, then drops to uid 10001 (see docker-entrypoint.sh).
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["ai-agent-channel", "--http", "--host", "0.0.0.0", "--port", "8765"]
