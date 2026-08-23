# Multi-stage: dependencies resolve once against a locked set, and the runtime
# image carries no build tooling, no uv, and no compiler.
FROM python:3.13-slim AS builder

# uv resolves and installs from the lockfile. Copying only the manifests first
# means a source-only change reuses this layer instead of re-resolving.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev


FROM python:3.13-slim AS runtime

# Run as a non-root user. The service opens sqlite target files by path, so the
# blast radius of a path-traversal bug is bounded by what this user can read.
RUN useradd --create-home --uid 10001 analyzer

WORKDIR /app
COPY --from=builder --chown=analyzer:analyzer /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # A container filesystem is ephemeral, so a sqlite app-state database is
    # silently recreated empty on every deploy. Default to refusing that
    # rather than losing saved queries on restart.
    FAE_DB_BACKEND=neon \
    # sqlite *targets* cannot work in a container either: the path is resolved
    # against this filesystem, so a path from a developer's laptop resolves to
    # nothing. Empty allowlist refuses them with a clear error.
    FAE_SQLITE_ALLOWED_DIRS="" \
    FAE_LOG_JSON=true

USER analyzer
EXPOSE 8000

# Readiness, not liveness: this one actually touches the database.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
