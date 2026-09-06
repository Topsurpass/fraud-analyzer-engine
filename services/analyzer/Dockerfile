# Multi-stage: dependencies resolve once against a locked set, and the runtime
# image carries no build tooling, no uv, and no compiler.
#
# Build context is this directory (services/analyzer), which is where
# pyproject.toml and uv.lock live:
#     docker build -t fae services/analyzer
# fly.toml sits beside this file so `fly deploy` uses the same context.
#
# The base is pinned by digest, not just by tag, so a rebuild six months from
# now installs the same interpreter and the same libc. Bump it deliberately:
#     docker buildx imagetools inspect python:3.13-slim --format '{{.Manifest.Digest}}'
ARG PYTHON_IMAGE=python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a

FROM ${PYTHON_IMAGE} AS builder

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


FROM ${PYTHON_IMAGE} AS runtime

# Run as a non-root user. The service opens sqlite target files by path, so the
# blast radius of a path-traversal bug is bounded by what this user can read.
RUN useradd --create-home --uid 10001 analyzer

WORKDIR /app

# Deliberately NOT --chown: the code and the virtualenv stay root-owned and
# read-only to the service account, so a bug in a service that executes
# user-supplied SQL cannot rewrite its own source. Everything the process
# writes goes in the two directories created below, which are the only
# writable paths in the image.
COPY --from=builder /app /app

# .secrets/fernet.key is written on startup when FAE_FERNET_KEY is unset, and
# /app is root-owned, so without this the container dies at boot with
# PermissionError: '.secrets'. data/ is here for the same reason: it is where a
# sqlite app-state database lands if someone overrides FAE_DB_BACKEND.
# Both are relative to WORKDIR, which is what app/security/crypto.py resolves
# against. Mount a tmpfs or a volume on them to run with --read-only.
RUN install -d -m 0700 -o analyzer -g analyzer /app/.secrets \
 && install -d -m 0700 -o analyzer -g analyzer /app/data

# One trust store that covers both the public web and Amazon RDS.
#
# TLS verification happens in this process, so the CA bundle has to be a path
# inside the container: a bundle downloaded onto the EC2 host is invisible from
# in here. Two sources are concatenated because neither is sufficient alone.
#
#   /etc/ssl/certs/ca-certificates.crt  Debian's Mozilla root set. Covers Neon,
#                                       Supabase, and the newer RDS
#                                       certificates that chain to Amazon Root
#                                       CA 1.
#   certs/rds-global-bundle.pem         AWS's own bundle: 108 regional RDS
#                                       roots, some of them self-signed and
#                                       present in no public trust store. An
#                                       instance still on one of those fails
#                                       verify-full against the system bundle.
#
# The union is a strict superset of the system bundle, so pointing at it can
# only make more certificates verify, never fewer. 0444: the service account
# reads it, nothing writes it.
RUN cat /etc/ssl/certs/ca-certificates.crt /app/certs/rds-global-bundle.pem \
      > /app/certs/trust-bundle.pem \
 && chmod 0444 /app/certs/trust-bundle.pem

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # A container filesystem is ephemeral, so a sqlite app-state database is
    # silently recreated empty on every deploy. Default to refusing that
    # rather than losing saved queries on restart.
    FAE_DB_BACKEND=neon \
    # If someone overrides the backend anyway, put the file somewhere the
    # service account can actually write.
    FAE_SQLITE_APP_DB_PATH=/app/data/fraud_analyzer.db \
    # sqlite *targets* cannot work in a container either: the path is resolved
    # against this filesystem, so a path from a developer's laptop resolves to
    # nothing. Empty allowlist refuses them with a clear error.
    FAE_SQLITE_ALLOWED_DIRS="" \
    # Where a verifying TLS mode on a *target* connection finds its CA when the
    # connection names no certificate of its own. A superset of the system
    # store, so this is never worse than leaving it unset. The app-state
    # database points at the same file, but through sslrootcert= in
    # DATABASE_URL, because that URL is libpq's to parse and not this app's.
    FAE_TARGET_SSL_ROOT_CERT=/app/certs/trust-bundle.pem \
    FAE_LOG_JSON=true

USER analyzer
EXPOSE 8000

# Readiness, not liveness: this one actually touches the database. It uses the
# interpreter rather than curl or wget, neither of which exists in a slim base.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=4).status==200 else 1)"

# Exec form: uvicorn is PID 1 and gets SIGTERM directly, so `docker stop` and a
# Fly machine shutdown drain in milliseconds instead of waiting out the kill
# timeout.
#
# WEB_CONCURRENCY sets the worker count, defaulting to 1. uvicorn reads it
# itself, so nothing has to be threaded through here.
#
# ## Why the default is 1 and why you may want more
#
# A poll is per-cell Python and holds the GIL, so one worker is one core, and
# a busy board saturates it: measured against this container at 25,000 rows a
# query, one analyst polling saw 29 ms and ten concurrent analysts saw 200 ms,
# which is one core running out rather than anything queueing badly.
#
# More workers buy real parallelism, at a real cost: the result cache and the
# rendered-response cache are per process, so N workers means a given result is
# fetched and rendered up to N times instead of once, and N times the memory
# holding it. That trade is worth making on a multi-core host with several
# analysts and is not worth making on a single-core one, which is why it is a
# deployment decision rather than a baked-in number. Start at the core count
# and watch the cache hit ratio, not the CPU graph.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
