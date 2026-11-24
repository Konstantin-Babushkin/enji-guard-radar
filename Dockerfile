# syntax=docker/dockerfile:1
# The radar's `serve` command in a small image. Nothing secret is built in: config, the env file
# and the state directory are mounted at run time (see compose.yaml and docs/deploy.md).
#
# ASSUMPTION: the uv image tag below exists (it matches the uv that wrote uv.lock); it is first
# exercised by a real `docker compose build`.
FROM ghcr.io/astral-sh/uv:0.12.21 AS uv

# Build stage: install exactly what the reviewed uv.lock pins, project included, into /app/.venv.
FROM python:3.12-slim-bookworm AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# Final stage: only the virtualenv, as a non-root user. The installed entry point runs directly
# (exec form, so SIGTERM reaches the radar); no uv, no resolver, no network access at run time.
FROM python:3.12-slim-bookworm
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin radar \
 && mkdir /state \
 && chown 10001:10001 /state \
 && chmod 0700 /state
COPY --from=build /app/.venv /app/.venv
# Log lines and the run summary must reach `docker logs` at once.
ENV PYTHONUNBUFFERED=1
USER 10001:10001
WORKDIR /state
ENTRYPOINT ["/app/.venv/bin/enji-guard-radar"]
CMD ["serve", "--config", "/config/radar.yaml", "--state-dir", "/state", "--env-file", "/run/radar.env"]
