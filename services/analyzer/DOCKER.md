# Running the analyzer in Docker

Every command here was executed against this image, on this commit. Where a
command produces an error, the error text is copied from a real run.

`README.md` explains what the service does and how the safety layer works.
This file only covers running it in a container.

> **Deploying to a server? Read [`deploy/README.md`](../../deploy/README.md)
> instead.**
>
> This file is about running the analyzer container by itself: on a laptop, for
> development, or as one piece of a larger stack. Its "3. Production" section
> predates the service having user accounts, and puts HTTP basic authentication
> in front of an API that had none of its own. That is no longer the right
> shape: the analyzer now has real accounts and sessions, the dashboard is the
> front door, and a basic-auth prompt in front of it would break the dashboard's
> own sign-in without adding anything.
>
> The current production deployment - EC2, RDS, the dashboard, Caddy, and the
> scripts that check all of it - lives in `deploy/`.

## Prerequisites

| | Minimum | Why |
|---|---|---|
| Docker Engine | 20.10 | `--platform`, BuildKit, healthcheck `start-period` |
| Docker Compose | **v2.24** | `depends_on.condition: service_healthy` is v2-only, and `ports: !override` in `deploy/docker-compose.rehearsal.yml` needs 2.24 |
| Disk | ~1 GB | 254 MB image, plus the Postgres image and its volume |

```bash
docker version --format 'Docker {{.Server.Version}}'
docker compose version
```

Verified on Docker 27.5.1 and Compose v2.28.1. **Compose v1 (`docker-compose`,
with a hyphen) will not work**: it ignores `depends_on.condition`, so the
analyzer starts before Postgres accepts connections and exits. If
`docker compose version` prints a `1.x` version or the command is not found,
install the Compose v2 plugin before going further.

## Contents

- [Quickstart](#quickstart)
- [Before you expose this](#before-you-expose-this): the console, loopback, read-only roles
- [The reverse proxy](#the-reverse-proxy): what it must do, and where the config lives
- [Concepts](#concepts): build context, baked settings, writable paths, health vs ready, logs
- [1. Local: first run](#1-local-first-run)
- [2. Development](#2-development): compose, SQLite, a Postgres by hand, managed
- [3. Production](#3-production): see `deploy/`; the container-level settings that fail quietly
- [Image delivery](#image-delivery): registries, tags, cross-platform builds
- [Operating](#operating): backups, log rotation, draining
- [Configuration reference](#configuration-reference): every `FAE_` variable
- [Troubleshooting](#troubleshooting)
- [Cleaning up](#cleaning-up)

---

## Quickstart

### Development, with a real Postgres (the normal path)

`docker-compose.yml` in this directory runs the analyzer and the Postgres it
stores its own state in, with the analyzer gated on the database being ready.

```bash
cd services/analyzer

# Generate a Fernet key ONCE, into .env, and never again. This command is
# idempotent: run it every day and it still writes only the first time.
#
# Do not `export FAE_FERNET_KEY=$(openssl rand ...)` instead. That mints a new
# key on every invocation, and a shell-exported value overrides .env for compose
# interpolation - so starting the stack from a shell that has it and one that
# does not gives you two different keys. Every credential saved under the other
# one then fails to decrypt, on a connection whose every visible field looks
# correct. The engine now says so at startup, naming the connections it cannot
# read, but the cure is to not create the situation.
grep -q '^FAE_FERNET_KEY=' .env 2>/dev/null || \
  echo "FAE_FERNET_KEY=$(openssl rand -base64 32 | tr '+/' '-_')" >> .env

# Port 8000 is contended on a dev box. If it is taken, pick another:
#   export FAE_HOST_PORT=8080
docker compose up -d

# The analyzer takes about 8 seconds to migrate and open its port. Wait for it
# rather than curling immediately.
until curl -sf localhost:${FAE_HOST_PORT:-8000}/health >/dev/null; do sleep 1; done
echo ready
```

The API is on http://127.0.0.1:8000, docs at
http://127.0.0.1:8000/docs. Loopback only, by design; see
[Before you expose this](#before-you-expose-this).

```bash
docker compose ps          # both services should read (healthy)
docker compose logs -f analyzer
docker compose down        # stop; add -v to also delete the database
```

### Just looking around, no database

One container, SQLite app-state, nothing persisted.

```bash
cd services/analyzer
docker build -t fae .

docker run -d --name fae-look -p 127.0.0.1:8000:8000 \
  -e FAE_DB_BACKEND=sqlite \
  -e FAE_LOG_JSON=false \
  fae

until curl -sf localhost:8000/health >/dev/null; do sleep 1; done

curl -s localhost:8000/health   # {"status":"ok"}
curl -s localhost:8000/ready    # {"status":"ready"}
docker logs fae-look

docker rm -f fae-look           # everything it stored dies here
```

`FAE_DB_BACKEND=sqlite` is required: the image defaults to `neon` and exits
without it. `FAE_LOG_JSON=false` gives readable lines instead of JSON. Change
both `8000`s if that port is taken; see
[the port collision entry](#port-is-already-allocated-and-then-the-name-is-in-use).

[Section 1](#1-local-first-run) explains what this container does and does not
keep. [Section 2](#2-development) is where to go for anything you will run more
than once.

---

## Before you expose this

**This section described a service with no authentication. That is no longer
true**, and the change matters more than a doc correction: the old advice was
to put HTTP basic auth in front of the API, which today breaks sign-in rather
than protecting anything.

The analyzer now has user accounts, sessions, and an audit log. Administrators
are created with `fae create-admin`, deliberately never over HTTP. What follows
is what is still true about exposing this container.

**1. The interactive console is still served on the API port.** `/docs`,
`/redoc` and `/openapi.json` are a form that composes and executes SQL against
a configured database. Authentication gates the endpoints they call, but the
console itself is an invitation and does not belong on the internet. In the
production stack it is unreachable because the analyzer publishes no port at
all; `deploy/verify.sh` checks that all three return 404 from the public
address.

**2. Publish on loopback, never on `0.0.0.0`.** `-p 8000:8000` binds every
interface on the host. Verified: with `-p 8000:8000` the API answered on this
machine's LAN address; with `-p 127.0.0.1:8000:8000` the same request was
refused, while loopback still returned 200. Better still, publish no port and
reach it over a container network - which is what the production stack does.

**3. Use a read-only database role for every target.** The service blocks
writes at three layers, and a read-only role is the control that still holds if
one of those layers has a bug. See the read-only role section in
[`README.md`](README.md#use-a-read-only-database-role).

**4. `FAE_CORS_ORIGINS` is not access control.** It constrains browsers. It
does nothing to `curl`, and nothing to anything that is not a browser.

**5. Rate limits are per worker process.** With `WEB_CONCURRENCY=2` the
effective limit is twice what the setting says. It is a containment control,
not accounting.
## The reverse proxy

**The working proxy configuration is [`deploy/Caddyfile`](../../deploy/Caddyfile),
used by [`deploy/docker-compose.prod.yml`](../../deploy/docker-compose.prod.yml).**

`Caddyfile` and `docker-compose.proxy.yml` used to sit in this directory and
have been removed. They put HTTP basic authentication in front of the API,
which was right when the service had no authentication of its own and is wrong
now: the analyzer has accounts and sessions, the dashboard is the front door,
and a basic-auth prompt in front of that breaks its sign-in without adding
anything. Leaving a working-looking config that breaks the deployment is worse
than leaving none.

What the current proxy does, and what any replacement has to do:

- **Terminates TLS.** The session cookie is `Secure`, so a browser will not
  store it over plain HTTP - sign-in silently fails and returns to the login
  page with no error. Port 80 exists only to redirect.
- **Compresses.** `encode zstd gzip`. A 25,000-row result is 2.87 MB of JSON
  that compresses about tenfold, and this is the only hop that does it.
- **Is the only thing with a published port.** The analyzer publishes none. It
  is reachable solely on the internal network, from the dashboard, which is
  what keeps the console below off the internet without depending on a path
  rule staying correct.
- **Sets `X-Forwarded-For` itself** rather than passing through what the caller
  sent, since the rate limiter reads it.

Nothing there is Caddy-specific in intent. If you already run nginx or Traefik,
reproduce those four properties.
## Concepts

### The build context is `services/analyzer`, not the repo root

```bash
cd services/analyzer
docker build -t fae .
```

The build does `COPY pyproject.toml uv.lock ./`, and both files live in this
directory. Build from the repo root and there is nothing to copy. `fly.toml`
sits beside the `Dockerfile` for the same reason: Fly's build context is the
directory holding `fly.toml`, so the two contexts stay identical.
`tests/test_container_layout.py::test_every_fly_build_context_has_what_its_dockerfile_copies`
fails the gate lane if that ever drifts again.

The base image is pinned by digest (`python:3.13-slim@sha256:ffb752e1...`), so
a rebuild of this commit in six months installs the same interpreter and the
same libc. Bump it deliberately:

```bash
docker buildx imagetools inspect python:3.13-slim --format '{{.Manifest.Digest}}'
```

`.dockerignore` keeps `.env`, `.secrets/`, `*.db`, `*.key`, `*.pem`, `.git`,
`tests/`, `docker-compose.yml`, and every `__pycache__` out of the context. Two
of those matter for more than image size:

- A host `__pycache__` copied into the image shadows the source it was built
  from, so the container can run code that no longer exists on disk.
- Prose is excluded as `**/*.md`. Editing this file or `README.md` must not
  invalidate the layer that installs dependencies, which is a three-minute
  rebuild. Verified: after adding this file and editing `README.md`, a rebuild
  was 2.9 seconds with every layer `CACHED`. Nothing in the build reads
  Markdown: `pyproject.toml` declares no `readme`, hatchling packages only
  `app/`, and `alembic/README` has no extension.

`alembic/` and `alembic.ini` are deliberately **not** ignored. `FAE_AUTO_MIGRATE`
defaults to `true`, so the container runs `alembic upgrade head` against its own
app-state database at startup and needs the migration scripts inside the image.

### What the image bakes in, and why

The runtime stage sets four environment variables that differ from the code
defaults in `app/config.py`. Every one exists because the code default is right
on a laptop and wrong in a container.

| Variable | Code default | Image default | Why the image overrides it |
|---|---|---|---|
| `FAE_DB_BACKEND` | `sqlite` | `neon` | A container filesystem is ephemeral, so a SQLite app-state file is silently recreated empty on every deploy. The image refuses to start rather than lose saved queries quietly. |
| `FAE_SQLITE_APP_DB_PATH` | `./fraud_analyzer.db` | `/app/data/fraud_analyzer.db` | `/app` is root-owned. If someone overrides the backend anyway, the file has to land where the service account can write. |
| `FAE_SQLITE_ALLOWED_DIRS` | `.` | `""` (empty) | A SQLite *target* path is resolved against the container filesystem, so a path from a developer's laptop points at nothing. An empty allowlist refuses them with a clear error instead of a confusing one. |
| `FAE_LOG_JSON` | `false` | `true` | Structured lines for a log shipper. Set it back to `false` when you are reading logs yourself. |

The image runs as uid 10001 (`analyzer`), created with
`useradd --create-home --uid 10001 analyzer`. The application code and the
virtualenv are copied **without** `--chown`, so they stay root-owned and
read-only to the service account. A service whose whole job is executing
caller-supplied SQL should not be able to rewrite its own source.

`uv` and the compiler live in the builder stage only. The runtime layer has
neither.

The image does **not** run with a read-only root filesystem on its own. Nothing
in the Dockerfile can do that; `--read-only` is a flag the operator passes at
`docker run`. What the image does is make it possible, by keeping every write
in two directories you can avoid using. See
[3. Production](#3-production).

### Environment variable precedence

Settings come from `app/config.py`, `env_prefix="FAE_"`. Every field is
`FAE_<FIELD_NAME_UPPERCASED>`.

Two exceptions to know:

1. **`DATABASE_URL` has no prefix.** The field declares
   `AliasChoices("FAE_DATABASE_URL", "DATABASE_URL")`, because `DATABASE_URL`
   is the name every managed Postgres host injects. Both spellings work;
   `FAE_DATABASE_URL` is checked first.
2. **`FAE_APP_DB_URL` beats `FAE_DB_BACKEND` entirely.** `resolved_app_db_url`
   returns it verbatim (after the psycopg scheme rewrite) and never looks at
   the backend switch. It is an escape hatch, mainly for tests. If you set it
   by accident, the startup banner says `from FAE_APP_DB_URL override`, which
   is exactly what that field of the banner is for.

Otherwise the order is the ordinary one: `docker run -e` and `--env-file` beat
the image's `ENV`, which beats the code defaults. The `.env` file that
`SettingsConfigDict(env_file=".env")` reads is never inside the image, because
`.dockerignore` excludes `**/.env`.

A `postgresql://` or `postgres://` URL is rewritten to `postgresql+psycopg://`
automatically (`normalize_pg_url`), so a connection string can be pasted in
unedited.

### The two writable paths

Exactly two directories in the image are writable by uid 10001. Both are
created by the Dockerfile with `install -d -m 0700 -o analyzer -g analyzer`.

| Path | What lands there | When |
|---|---|---|
| `/app/.secrets` | `fernet.key` | Only when `FAE_FERNET_KEY` is unset |
| `/app/data` | `fraud_analyzer.db` | Only when the app-state backend resolves to SQLite |

`.secrets` is relative to `WORKDIR` because `app/security/crypto.py` resolves
`Path(".secrets")` against the working directory. Before those `install -d`
lines existed the container died at boot with `PermissionError: '.secrets'`.
`tests/test_container_layout.py::test_every_path_the_app_writes_to_is_created_writable`
reads `crypto.KEY_DIR` and asserts the Dockerfile still creates it.

Set `FAE_FERNET_KEY` and use a Postgres backend and neither path is touched, so
you can pass `--read-only`.

### Health is not readiness

| Endpoint | Checks | Use it for |
|---|---|---|
| `GET /health` | Nothing. Returns `{"status":"ok"}` as long as the process is alive. | Liveness |
| `GET /ready` | `SELECT 1` against the app-state database. 503 `SERVICE_NOT_READY` when it fails. | Readiness, load balancer admission |

`/health` deliberately touches nothing. A liveness probe that fails during a
database outage makes the orchestrator kill and reschedule a process that was
working, turning a dependency outage into a restart loop.

The image's `HEALTHCHECK` polls `/ready`, not `/health`, so `docker ps` reports
`unhealthy` when the database is unreachable even though the process is fine.
That is intentional and is
[covered in troubleshooting](#docker-ps-says-unhealthy-but-health-returns-200).
It uses `python -c` rather than `curl` or `wget`, neither of which exists in a
slim base.

`start-period` is 60s. A suspended serverless Postgres can take over ten
seconds just to accept a connection, and startup then runs Alembic before
serving, so a short grace kills the container mid-migration.

### What the logs actually look like

`FAE_LOG_JSON` controls **the service's own logger**, not everything on stdout.
Two streams share the container's output:

- **The service's root handler** (`app.*`, `alembic.*`) honours
  `FAE_LOG_JSON` and `FAE_LOG_LEVEL`. With JSON on, each line is an object with
  `ts`, `level`, `logger`, `request_id`, `message`, and `exception` when there
  is one.
- **uvicorn's own logger** has its own configuration and is always plain text.
  These four lines appear on every boot regardless of `FAE_LOG_JSON`:

```
INFO:     Started server process [1]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

The plain-text set is fixed: the four banner lines above, plus one uvicorn
access line per request. The JSON count varies with what the boot did.
Measured with `FAE_LOG_JSON=true` and three requests served, on the same
volume:

| Boot | JSON | plain text |
|---|---|---|
| First (four `alembic upgrade` lines) | 14 | 7 |
| Second (schema already at head) | 10 | 7 |

The four-line difference is exactly the migrations. Do not read a specific
total as a constant; read the ratio, and read which lines fall on which side.
Every plain-text line comes from uvicorn, and uvicorn's access log duplicates
the service's own access line:

```
INFO:     127.0.0.1:53864 - "GET /ready HTTP/1.1" 200 OK        <- uvicorn, plain text
{"ts": "...", "logger": "app.access", "request_id": "a1b2...",
 "message": "GET /ready -> 200 in 5ms"}                          <- the service, JSON
```

**For a log shipper this means two things.** Configure it to tolerate
non-JSON lines rather than erroring on them, and parse the `app.access` line,
not uvicorn's: only the service's line carries `request_id`, which is the same
value the response returns in `X-Request-ID`.

You can drop uvicorn's duplicate access line by overriding the command:

```bash
docker run ... fae uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log
```

Verified: that takes the plain-text count from 7 to 4, leaving only the four
banner lines, and loses nothing, because `app.access` already logs every
request with method, path, status, duration, and request id.

### Confirming which backend actually resolved

The first line of the startup log names the resolved backend, where the setting
came from, the URL with the password redacted, and whether migrations ran:

```
App-state backend=neon (from FAE_DB_BACKEND) url=postgresql+psycopg://USER:***@HOST/DB auto_migrate=True
```

```bash
docker logs <container> 2>&1 | grep 'App-state backend'
```

If that says `backend=sqlite` on a deployment you believe is on Postgres, the
deployment is not configured and its data will not survive a restart.

This line is `INFO`. If you raise `FAE_LOG_LEVEL` above `INFO` you lose it.

---

## 1. Local: first run

The command is
[in the Quickstart](#just-looking-around-no-database); this section is what it
does, what it keeps, and what it throws away.

Two flags in it are load-bearing. `-d --name fae-look` keeps the terminal yours
and gives every later command something to refer to, which matters because the
next three subsections all say `docker logs <name>`. `FAE_DB_BACKEND=sqlite`
overrides the image's `neon` default; without it the container exits
immediately (see
[the first troubleshooting entry](#fae_db_backendneon-requires-database_url-to-be-set)).

**Three warnings fire at startup, and all three are correct:**

```
WARNING  app.db.migrate: App-state is SQLite at /app/data/fraud_analyzer.db. On a container with an ephemeral filesystem every saved connection and query is lost on restart. ...
WARNING  app.security.crypto: FAE_FERNET_KEY was not set. Generated a development key at /app/.secrets/fernet.key. ...
WARNING  app.db.migrate: FAE_FERNET_KEY is not set, so a credential encryption key was generated on local disk. If this filesystem is ephemeral, every stored target-database password becomes permanently undecryptable on the next restart. ...
```

The last two are the same fact from two places: `crypto.py` reports generating
the key, and `migrate.py` reports the consequence. The `migrate.py` one is the
line the production checklist tells you to grep for, so seeing it here is what
it looks like when it is expected.

**State dies with the container.** `docker rm` deletes the writable layer, so
`/app/data/fraud_analyzer.db` and `/app/.secrets/fernet.key` go with it. To
keep the database, add a named volume:

```bash
docker run -d --name fae-look -p 127.0.0.1:8000:8000 \
  -v fae-data:/app/data \
  -e FAE_DB_BACKEND=sqlite \
  -e FAE_LOG_JSON=false \
  fae
```

`/app/data` is already where `FAE_SQLITE_APP_DB_PATH` points in the image, so
the mount needs no other setting. A named volume is seeded from the image
directory it covers, so it inherits mode 0700 and uid 10001 and needs no chown.

**Expect the "App-state is SQLite" warning even with the volume mounted.**
`warn_if_storage_is_ephemeral()` fires on any SQLite backend and does not know
what you mounted, so it still suggests mounting a volume at that path. Verified
with `-v fae-data:/app/data`: the warning is unchanged. On SQLite it is advice;
only on a deployment you believe is on Postgres is it a failure signal.

The volume keeps the database but not the key: `.secrets` is still on the
container layer. Set `FAE_FERNET_KEY` as soon as you save a real connection.

### Verify

```bash
# Wait for it. About 8 seconds; sooner than that gives curl exit 56 and an
# empty grep, which reads like a failure and is not.
until curl -sf localhost:8000/health >/dev/null; do sleep 1; done

curl -s localhost:8000/health   # {"status":"ok"}
curl -s localhost:8000/ready    # {"status":"ready"}
docker logs fae-look 2>&1 | grep 'App-state backend'
# App-state backend=sqlite (from FAE_DB_BACKEND) url=sqlite:////app/data/fraud_analyzer.db auto_migrate=True
```

`docker ps` is the same signal without the loop: `(health: starting)` means
wait, `(healthy)` means the container has answered `/ready` itself.

Interactive docs at http://127.0.0.1:8000/docs.

---

## 2. Development

Development wants three things the first-run recipe does not give you:
persistence across container restarts, readable logs, and a backend close
enough to production that a bug shows up here instead of there.

| Working on | Use |
|---|---|
| Anything that touches the database, migrations, or a frontend | [2a, compose](#2a-docker-compose-the-primary-path). This is the default. |
| API shape, routers, the SQL guard, nothing persistent | [2b, SQLite on a volume](#2b-sqlite-on-a-named-volume) |
| Inspecting the `.db` file with a SQLite browser | [2c, bind mount](#2c-sqlite-on-a-bind-mount) |
| Understanding what compose is doing for you | [2d, the same thing by hand](#2d-the-same-stack-by-hand) |
| A shared or managed development database | [2e](#2e-a-managed-postgres-development-database) |

Every recipe sets `FAE_LOG_JSON=false`. The image ships `true`.

### 2a. `docker compose`, the primary path

The commands are in the [Quickstart](#development-with-a-real-postgres-the-normal-path);
this section is what they are doing and how to live with them.

Measured cold, with the image already built: 27 seconds to both services
healthy, including waiting for Postgres.

```
 Container fae-postgres-1  Starting
 Container fae-postgres-1  Started
 Container fae-postgres-1  Waiting
 Container fae-postgres-1  Healthy
 Container fae-analyzer-1  Starting
 Container fae-analyzer-1  Started
```

The `Waiting` and `Healthy` lines are the part that matters. `docker-compose.yml`
declares:

```yaml
depends_on:
  postgres:
    condition: service_healthy
```

Plain `depends_on: [postgres]` waits for the container to be *created*, not for
Postgres to accept connections. The analyzer runs Alembic during startup, so it
connects immediately and exits non-zero if the database is not listening yet.
The `condition: service_healthy` form is what makes `up -d` reliable rather
than a race you win most of the time.

`openssl rand -base64 32 | tr '+/' '-_'` produces a valid Fernet key. Verified:
44 characters, accepted by `Fernet()`, round-trips an encrypt/decrypt. Save the
value and reuse it. A new key on every `up` makes every credential saved during
the previous run undecryptable, which is why the compose file uses `:?` and
fails rather than defaulting.

**Compose reads `services/analyzer/.env`, and that is the application's
settings file.** Compose auto-loads `.env` from the project directory for
variable substitution. This directory already has one, holding real
credentials. Verified: with `FAE_FERNET_KEY` unset in the shell, compose still
resolved it, from `.env`, and the `:?` guard never fired. Two consequences:

- Convenient, but invisible. To see what compose resolves without that file:
  `docker compose --env-file /dev/null config`. With the shell variable unset
  that correctly fails with
  `required variable FAE_FERNET_KEY is missing a value`.
- **Never write `${DATABASE_URL}` into this compose file.** It would silently
  substitute whatever `DATABASE_URL` is in `.env`, which on a configured
  machine is a production database, and point your dev stack at it. The
  compose file hardcodes the `postgres` service URL for exactly this reason.

`FAE_FERNET_KEY` is interpolated before anything runs, so **every** compose
subcommand needs it in the environment, `docker compose build` and
`docker compose down` included, not just `up`. Export it in your shell profile
or keep it in `.env`.

Day-to-day:

```bash
docker compose ps                    # both should read (healthy)
docker compose logs -f analyzer
docker compose up -d --build         # rebuild after a source change
docker compose restart analyzer
docker compose down                  # stop and remove containers, keep the database
docker compose down -v               # also delete the pgdata volume. Not reversible.
```

Overrides, all optional:

```bash
FAE_HOST_PORT=8080 docker compose up -d          # publish somewhere else
POSTGRES_PASSWORD=... docker compose up -d       # change the local database password
FAE_CORS_ORIGINS=http://localhost:5173 docker compose up -d
```

The Postgres port is deliberately not published. Nothing outside the compose
network needs it. If you want a psql session:

```bash
docker compose exec postgres psql -U fae -d fae
```

### 2b. SQLite on a named volume

No Postgres, state survives container recreation.

```bash
docker run -d --name fae-dev -p 127.0.0.1:8000:8000 \
  -v fae-data:/app/data \
  -e FAE_DB_BACKEND=sqlite \
  -e FAE_LOG_JSON=false \
  -e FAE_FERNET_KEY='<your saved key>' \
  fae
```

Generate the key once and keep it, with the same command the Quickstart uses.
Do **not** inline the generator into the `docker run`; that produces a new key
on every invocation and defeats the point.

```bash
openssl rand -base64 32 | tr '+/' '-_'
```

Docker creates `fae-data` on first use and seeds it from the image's
`/app/data`, which is mode 0700 owned by uid 10001, so the service can write to
it with no further setup. Destroy and recreate the container as often as you
like; the database survives. Verified across a `docker rm -f` and a fresh
`docker run`: same file, same 10001:10001 ownership, `/ready` 200.

### 2c. SQLite on a bind mount

Use this when you want the `.db` file in a directory you can open with a SQLite
browser. It has one caveat that costs people an hour, so here is the working
command first:

`./data` here is inside `services/analyzer`, and it is not gitignored, so it
shows up as untracked in `git status` until you remove it. Either put it
outside the repo (`-v "$HOME/fae-data":/app/data`) or add `data/` to
`.gitignore` before you forget. [Cleaning up](#cleaning-up) covers removing it.

```bash
mkdir -p ./data
docker run -d --name fae-dev -p 127.0.0.1:8000:8000 \
  --user "$(id -u):$(id -g)" \
  -v "$PWD/data":/app/data \
  -e FAE_DB_BACKEND=sqlite \
  -e FAE_LOG_JSON=false \
  -e FAE_FERNET_KEY='<your saved key>' \
  fae
```

Both `--user` and `FAE_FERNET_KEY` are load-bearing, and dropping either one
fails differently.

**Without `--user`**, a bind mount keeps the host directory's ownership, the
process is uid 10001, and it cannot create a file there:

```
sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) unable to open database file
```

**With `--user` but without the key**, you trade that for the other writable
path. `/app/.secrets` is mode 0700 owned by uid 10001, and your uid is not
10001:

```
PermissionError: [Errno 13] Permission denied: '.secrets/fernet.key'
```

Setting the key means nothing is ever written to `.secrets`, so the mode 0700
directory stops mattering. That is the same reason a production container can
run `--read-only`.

**The alternative to `--user`** is to give the directory to the service
account. That needs root, so it is yours to run:

```bash
# Run this yourself; it needs sudo.
sudo chown 10001:10001 ./data
```

Then the `docker run` above works without `--user`, and the container keeps its
own unprivileged identity. Prefer this on a shared or long-lived machine.
`--user` is faster when you are iterating and want the file readable by your
editor. Note that after the `chown` the directory is no longer yours, so
removing it later also needs sudo; see [Cleaning up](#cleaning-up).

### 2d. The same stack by hand

What compose does in 2a, spelled out. Useful when you are debugging the
networking rather than using it.

`localhost` inside a container is that container, not your machine, so a
`DATABASE_URL` pointing at `localhost:5432` cannot reach a Postgres running on
the host or in another container. Put both on a user-defined bridge network and
address the database by container name; Docker's embedded DNS resolves it. On
the default bridge, name resolution does not work.

```bash
docker network create fae-net

docker run -d --name fae-pg --network fae-net \
  -e POSTGRES_USER=fae \
  -e POSTGRES_PASSWORD=devpassword \
  -e POSTGRES_DB=fae \
  postgres:16

# Wait for it. pg_isready polls once and returns immediately; it does not wait.
# Run straight after the line above it prints "no response" and exits 2.
until docker exec fae-pg pg_isready -U fae -d fae >/dev/null 2>&1; do sleep 1; done

docker run -d --name fae-dev --network fae-net -p 127.0.0.1:8000:8000 \
  -e DATABASE_URL='postgresql://fae:devpassword@fae-pg:5432/fae' \
  -e FAE_LOG_JSON=false \
  -e FAE_FERNET_KEY='<your saved key>' \
  fae
```

Measured: `pg_isready` immediately after `docker run` returns
`/var/run/postgresql:5432 - no response`, exit 2, and the loop above succeeds
on the fourth attempt. The analyzer connects during startup, so starting it
before that loop finishes is a crash, not a retry.

Notes on that `DATABASE_URL`:

- The host is `fae-pg`, the container name, resolved over `fae-net`.
- The port is the container's own `5432`, not a published host port. Traffic
  never leaves the bridge, so the database needs no `-p` at all.
- `devpassword` is a local throwaway on a network with no published port. Do
  not carry this pattern into anything shared.
- No `sslmode=require`. Inside a docker bridge there is no TLS to require, and
  asking for it against a stock `postgres:16` fails the connection.
- `FAE_DB_BACKEND` is not set: the image already defaults to `neon`, and
  "neon" here just means "the Postgres at `DATABASE_URL`". Nothing in
  `config.py` is Neon-specific.

Tear down:

```bash
docker rm -f fae-dev fae-pg && docker network rm fae-net
```

### 2e. A managed Postgres development database

Same as production, pointed at a non-production database. Nothing about the
container changes.

```bash
docker run -d --name fae-dev -p 127.0.0.1:8000:8000 \
  -e DATABASE_URL='postgresql://USER:PASSWORD@HOST/DB?sslmode=require' \
  -e FAE_LOG_JSON=false \
  -e FAE_FERNET_KEY='<your saved key>' \
  fae
```

Use a separate Neon branch or a separate database, never the production one.
Startup runs `alembic upgrade head` against whatever `DATABASE_URL` names, and
`_prune_logs_on_startup` deletes execution logs past `FAE_LOG_RETENTION_DAYS`.
Neither is something you want aimed at production by accident.

Give the first connection room: `FAE_APP_DB_CONNECT_TIMEOUT_S` defaults to 30
because a suspended serverless instance can take well over ten seconds to
accept a connection. Being marked `unhealthy` for the first minute is the
`start-period` doing its job.

Use a **different** `FAE_FERNET_KEY` from production. The key and the database
travel together: a dev key against a production database makes every stored
credential unreadable, and a production key on a dev box is a production secret
on a dev box.

### Verify (any development recipe)

```bash
# Wait first. The analyzer takes about 8 seconds to migrate and open its port,
# so curling straight after `up -d` gives `curl: (56) Recv failure` and an
# empty log grep.
until curl -sf localhost:8000/health >/dev/null; do sleep 1; done

curl -s localhost:8000/health   # {"status":"ok"}
curl -s localhost:8000/ready    # {"status":"ready"}

# Which variables are set, without printing their values.
docker exec fae-dev env | grep -oE '^(FAE_[A-Z_]+|DATABASE_URL)=' | sort

docker logs fae-dev 2>&1 | grep 'App-state backend'
```

For compose, `docker compose exec analyzer` and `docker compose logs analyzer`.

**Then check it actually works, not just that it booted.** Create a connection
pointed at the compose Postgres and run a query through it. On the compose
stack the database is reachable from the analyzer as `postgres`:

```bash
curl -s -X POST localhost:8000/connections \
  -H 'Content-Type: application/json' \
  -d '{"name":"compose-postgres","db_type":"postgres","host":"postgres","port":5432,
       "database":"fae","username":"fae","password":"devpassword"}'
```

```json
HTTP 201
{"connection": {"id": "ce2370a3-...", "status": "ok", ...},
 "test_ok": true, "test_error": null}
```

`test_ok: true` is the answer. `POST /connections` opens the connection as part
of creating it, so a 201 with `test_ok: true` means credentials, networking,
and the driver all work. A 201 with `test_ok: false` still saves the profile
and puts the reason in `test_error`.

Then run SQL:

```bash
CID=$(curl -s localhost:8000/connections | python3 -c 'import sys,json; print(json.load(sys.stdin)[0]["id"])')

curl -s -X POST localhost:8000/connections/$CID/query/preview \
  -H 'Content-Type: application/json' -d '{"sql_text":"SELECT 1 AS n, 2 AS m"}'
# {"row_count":1,"truncated":false,"columns":["n","m"],"rows":[[1,2]], ...}

curl -s -X POST localhost:8000/connections/$CID/query/preview \
  -H 'Content-Type: application/json' -d '{"sql_text":"DROP TABLE connections"}'
# {"error_code":"NON_SELECT_STATEMENT","message":"Only SELECT statements are permitted; this is DROP."}
```

The field is `sql_text`, not `sql`; `sql` returns a 422 naming the missing
field. That second call is worth running once: it is the SQL guard refusing a
write, which is the property the whole service rests on.

`devpassword` here is the compose stack's local Postgres password. Behind the
proxy, add `-u analyst:<password>` and use `https://localhost:8443`.

The banner is what settles arguments. For 2a it reads:

```
App-state backend=neon (from FAE_DB_BACKEND) url=postgresql+psycopg://fae:***@postgres:5432/fae auto_migrate=True
```

The password is redacted by `describe_app_db()`, which renders the URL with
`hide_password=True`. A startup banner that leaked credentials into a hosting
provider's log aggregator would be worse than no banner. Do not undo that by
dumping the environment; see [the note on that](#printing-the-environment-prints-secrets).

---

## 3. Production

**The production deployment lives in [`deploy/`](../../deploy/README.md).** Not
here.

That directory holds the whole stack - the analyzer, the Next.js dashboard in
front of it, Caddy terminating TLS, app state in managed Postgres - plus
`preflight.sh`, `deploy.sh` and a `verify.sh` that makes 38 checks against the
running result. This file stops at the edge of one container.

What used to be written here was a single-container recipe from before the
service had user accounts: publish on loopback, put HTTP basic authentication
in front of it, block the console at the proxy. Following it today would break
the deployment rather than secure it - see [The reverse
proxy](#the-reverse-proxy).

Three container-level facts still belong here, because they are true wherever
this image runs and each one fails quietly rather than loudly.

**`FAE_FERNET_KEY` is the one that fails silently.** `crypto.py` resolves the
key in three steps: the environment variable, then `.secrets/fernet.key` on
disk, then generate one and write it there. Step three is what happens in
production when you forget. The generated key lands on the container
filesystem, which is wiped on the next deploy, while the Fernet-encrypted
target-database passwords sit in a durable Postgres. Redeploy and the service
comes up with a fresh key against ciphertext it can no longer read - every
stored credential unreadable, every visible field on the connection still
correct.

Back the key up somewhere that is not the instance. The startup log prints its
fingerprint (never the key); comparing that across two deploys is what turns
"the passwords broke again" into "these are two different keys".

**`DATABASE_URL` decides whether anything survives a restart.** The image sets
`FAE_DB_BACKEND=neon`, meaning "managed Postgres addressed by `DATABASE_URL`" -
the name predates the move to RDS and describes the shape, not the vendor.
Without the URL the container refuses to start, which is intended: a silent
fallback to SQLite would write saved queries to a container filesystem that is
wiped on the next deploy.

Set the TLS mode on that URL. `sslmode=verify-full` encrypts *and* checks the
server is who it claims to be; `require` only encrypts, and libpq's own default
(`prefer`) will silently accept an unencrypted connection. The image carries a
CA bundle at `/app/certs/trust-bundle.pem` covering the public roots plus AWS's
RDS roots. See "TLS to RDS" in `deploy/README.md`.

**Two writable paths, and only two.** `/app/.secrets` and `/app/data`. Run with
`--read-only` and mount a tmpfs or volume on both, or the container dies at
boot with `PermissionError: '.secrets'`.
## Image delivery

Outside Fly, which builds from source on every deploy, the image has to get to
the host somehow. A bare `docker build -t fae .` produces a tag that exists
only on the machine that ran it.

**Tag with something you can trace back to a commit.** `latest` tells you
nothing at 3am and cannot be rolled back to:

```bash
cd services/analyzer
TAG=0.1.0-$(git rev-parse --short HEAD)

docker build -t registry.example.com/fae/analyzer:$TAG .
docker tag registry.example.com/fae/analyzer:$TAG registry.example.com/fae/analyzer:latest

docker push registry.example.com/fae/analyzer:$TAG
docker push registry.example.com/fae/analyzer:latest
```

Then on the host, `docker pull registry.example.com/fae/analyzer:$TAG` and run
that exact tag, not `latest`, so a restart cannot silently pick up a different
build.

The build and tag steps above were executed here. The two `docker push` lines
were not: there is no registry configured on this machine, and they are the
standard form rather than anything specific to this image.

**Building on Apple Silicon for a Linux host.** `docker build` produces an
image for the machine you are on, so a build on an M-series Mac is
`linux/arm64` and will not run on an amd64 server. Name the target explicitly:

```bash
docker buildx build --platform linux/amd64 \
  -t registry.example.com/fae/analyzer:$TAG --push .
```

Check what you actually produced:

```bash
docker image inspect <image> --format 'Os/Arch: {{.Os}}/{{.Architecture}}'
# Os/Arch: linux/amd64
```

The base image is multi-arch, and every dependency installs from the lockfile
with `UV_PYTHON_DOWNLOADS=never`, so a cross-build resolves the same package
set. Cross-building under emulation is slower than a native build; if that
matters, build on an amd64 machine or a CI runner.

---

## Operating

### Backups

The app-state database holds every connection profile, saved query, dashboard,
and execution log. On a managed host use the provider's backups (Neon keeps
point-in-time history on its own schedule); that is the primary mechanism and
it is not something this container does.

For a self-hosted Postgres, dump it on a schedule. **Do not redirect straight
to the final filename.** The shell creates the file before `pg_dump` runs, so a
failure (wrong container name, database down, bad credentials) leaves a 0-byte
file that looks like a backup and overwrites yesterday's:

```bash
#!/bin/sh
set -eu
OUT="fae-$(date +%F).dump"

# Compose: the container is <project>-postgres-1, so address the SERVICE.
docker compose exec -T postgres pg_dump -U fae -d fae --format=custom > "$OUT.part"

# Only becomes the real backup if pg_dump exited 0 and wrote something.
[ -s "$OUT.part" ] || { echo "pg_dump produced nothing" >&2; rm -f "$OUT.part"; exit 1; }
mv "$OUT.part" "$OUT"
```

`set -e` plus the `.part` rename is the whole trick: a failed dump exits
non-zero and leaves no file named like a backup.

Two container-naming traps, both of which produce
`Error: No such container` and exit 1:

- On the compose stack the container is `<project>-postgres-1`
  (`fae-postgres-1` with the default project name), not `postgres`. Use
  `docker compose exec -T postgres` and let compose resolve it.
- On the by-hand stack from [2d](#2d-the-same-stack-by-hand) it is whatever you
  named it, `fae-pg` in that recipe: `docker exec fae-pg pg_dump ...`.

`-T` disables TTY allocation, which is required whenever the output is
redirected; without it compose fails with `the input device is not a TTY` in
cron.

Verified against the compose stack: exit 0, 11,907 bytes for a freshly migrated
empty schema. Restore with `pg_restore` into an empty database.

**Back up `FAE_FERNET_KEY` separately, and treat it as part of the backup.** A
database dump without the key is a dump in which every stored target-database
password is unreadable. Restoring one without the other gets you a working
service with dead connections.

For the compose stack the same data is in the `pgdata` volume;
`docker compose down -v` deletes it, which is why that flag is called out
everywhere it appears.

### Log rotation

Docker's default `json-file` log driver is **unbounded**. This service logs a
line per request, so a polling dashboard will fill a disk given enough time.
Cap it per container:

```bash
--log-driver json-file --log-opt max-size=10m --log-opt max-file=3
```

Or set the same as a daemon-wide default in `/etc/docker/daemon.json` so you
cannot forget it on a container. That file is root-owned, so editing it is
yours to do.

### Stopping and draining

`CMD` is exec form, so uvicorn is PID 1 and receives `SIGTERM` directly rather
than having it swallowed by a shell. Measured on a fully started container:

```
docker stop  ->  1378 ms, exit 0
docker stop  ->  1497 ms, exit 0
```

with `Shutting down` / `Waiting for application shutdown` /
`Application shutdown complete` in the log, and `target_registry.dispose_all()`
running in the lifespan teardown so pooled target connections close cleanly.

**A stop issued while the container is still starting does not drain.**
Measured: `docker stop` two seconds after `docker run`, while the lifespan was
still running Alembic, took **10,770 ms and exited 137**, meaning Docker's
10-second grace elapsed and it was killed. Migrations run before uvicorn
installs its signal handling, so there is a window at boot where SIGTERM has
nowhere to land. It is only a concern if something restarts containers in a
tight loop; wait for `/ready` before stopping, and give an orchestrator a stop
grace longer than your migration time (`--stop-timeout`, or compose's
`stop_grace_period`).

---

## Configuration reference

Every field in `app/config.py`. All are optional unless the Required column
says otherwise. The **Image** column is filled in only where the Dockerfile's
`ENV` differs from the code default.

### App-state database

| Variable | Default | Image | Required | When to change it |
|---|---|---|---|---|
| `FAE_DB_BACKEND` | `sqlite` | `neon` | Yes, as `neon` in production | `sqlite` for a laptop or a first-run container. `neon` (meaning any Postgres) for anything whose data must survive a restart. |
| `DATABASE_URL` (or `FAE_DATABASE_URL`) | unset | | Yes when backend is `neon` | The Postgres URL. Add `?sslmode=require` for anything over the public internet. `postgresql://` is rewritten to `postgresql+psycopg://` for you. |
| `FAE_SQLITE_APP_DB_PATH` | `./fraud_analyzer.db` | `/app/data/fraud_analyzer.db` | No | Only if you mount the SQLite database somewhere other than `/app/data`. The image default is already the writable directory. |
| `FAE_APP_DB_URL` | unset | | No | Escape hatch. Overrides `FAE_DB_BACKEND` completely and is used verbatim. Mainly for tests. Avoid in a deployment; it makes the banner harder to read. |
| `FAE_AUTO_MIGRATE` | `true` | | No | Set `false` if migrations are a separate release step, or if several instances start at once and you do not want them racing on `alembic upgrade head`. The schema check runs either way. |
| `FAE_APP_DB_CONNECT_TIMEOUT_S` | `30` | | No | Raise if your serverless Postgres takes longer than 30s to wake. Separate from `FAE_CONNECT_TIMEOUT_S` on purpose: a slow app-state connect is normal, a slow *target* connect means something is wrong. |

### Credential encryption

| Variable | Default | Image | Required | When to change it |
|---|---|---|---|---|
| `FAE_FERNET_KEY` | unset (generates one into `.secrets/fernet.key`) | | **Yes in production** | Always set it in production and on any container whose filesystem is ephemeral. 32-byte urlsafe base64, 44 characters. Losing or rotating it makes every stored target credential permanently undecryptable. Back it up with the database. |

### SQLite target connections

| Variable | Default | Image | Required | When to change it |
|---|---|---|---|---|
| `FAE_SQLITE_ALLOWED_DIRS` | `.` | `""` | No | Leave empty in any container. A SQLite target path is opened by the API process, so an unbounded value is an arbitrary-file-read primitive; in a container the path resolves against the container filesystem anyway and points at nothing. |

### HTTP

| Variable | Default | Image | Required | When to change it |
|---|---|---|---|---|
| `FAE_CORS_ORIGINS` | `*` | | **Yes in production** | Comma-separated explicit origins. Constrains browsers only; it is not access control. See [Before you expose this](#before-you-expose-this). |
| `FAE_RATE_LIMIT_PER_MINUTE` | `600` | | No | Per-IP budget for every request. `0` disables. Per process, so N instances multiply it by N. |
| `FAE_RATE_LIMIT_EXECUTION_PER_MINUTE` | `300` | | No | Per-IP budget for anything that opens a target connection. Must absorb a real dashboard: 12 cards at the default poll interval is 144 polls/min from one tab. |
| `FAE_MAX_REQUEST_BYTES` | `1048576` (1 MB) | | No | Checked against `Content-Length` before the body is read. `0` disables. Raise only if a legitimate payload is larger. |

### Execution and limits

| Variable | Default | Image | Required | When to change it |
|---|---|---|---|---|
| `FAE_QUERY_TIMEOUT_S` | `10` | | No | Server-side statement timeout on target databases. |
| `FAE_CONNECT_TIMEOUT_S` | `10` | | No | Connect timeout for target databases. `POST /connections` tests the connection while creating it, so this bounds how long that call can block. |
| `FAE_SOCKET_TIMEOUT_GRACE_S` | `5` | | No | Client socket deadline is `FAE_QUERY_TIMEOUT_S` plus this. Keep it positive: if the socket and the statement timeout fire together, the socket usually wins and a slow query is reported as a lost connection (502) instead of a timeout (504). |
| `FAE_DEFAULT_ROW_LIMIT` | `1000` | | No | Rows returned when a request does not ask for a limit. |
| `FAE_MAX_ROW_LIMIT` | `10000` | | No | Ceiling a request may ask for. Validated to be at least `FAE_DEFAULT_ROW_LIMIT`. |
| `FAE_PREVIEW_ROW_LIMIT` | `100` | | No | Rows for the ad-hoc preview endpoint. |
| `FAE_MAX_SQL_LENGTH` | `8000` | | No | Bounds how much CPU one statement can spend in `sqlparse`, which is pure Python and superlinear in token density. Raising it above ~10,000 costs nothing (sqlparse bails at its own token ceiling); lowering it below ~8,000 is the only way to cut the worst case. |
| `FAE_SQL_VALIDATION_CACHE_SIZE` | `512` | | No | Distinct validated statements memoised, so a saved query pays parsing once instead of on every poll. `0` disables. |
| `FAE_MAX_RESULT_BYTES` | `33554432` (32 MB) | | No | Ceiling on one result payload, measured while rows are coerced. Row count says nothing about row width. Counts against `--memory`; raise both together or neither. |
| `FAE_CACHE_MAX_BYTES` | `67108864` (64 MB) | | No | Poll result cache budget. Bounding it by entry count instead measured out at roughly 1 GB for wide results. Counts against `--memory`; see [3. Production](#3-production). |
| `FAE_POLL_INTERVAL_MS` | `5000` | | No | Interval the API advertises to a polling dashboard. |

### Target connection pooling

| Variable | Default | Image | Required | When to change it |
|---|---|---|---|---|
| `FAE_TARGET_POOL_SIZE` | `10` | | No | Connections held per target database. |
| `FAE_TARGET_MAX_OVERFLOW` | `5` | | No | Extra connections above the pool size under load. |
| `FAE_MAX_TARGET_ENGINES` | `32` | | No | Distinct target engines kept alive. Lower it if you have many connection profiles and a small memory budget. |
| `FAE_TARGET_POOL_TIMEOUT_S` | `5` | | No | How long a request waits for a pooled connection. SQLAlchemy's default of 30s outlives the frontend's poll deadline and turns pool exhaustion into a hang instead of an error. |

### Logging and retention

| Variable | Default | Image | Required | When to change it |
|---|---|---|---|---|
| `FAE_LOG_LEVEL` | `INFO` | | No | Raising this above `INFO` hides the startup banner that names the resolved backend. |
| `FAE_LOG_JSON` | `false` | `true` | No | Controls the service's own logger only; uvicorn's four banner lines and its access log stay plain text either way. |
| `FAE_LOG_RETENTION_DAYS` | `30` | | No | Execution logs are pruned at startup past this age. One card polling at the default interval writes roughly 17k rows a day on cache misses, forever. `0` disables pruning. |
| `FAE_MAX_LOGS_PER_QUERY` | `1000` | | No | Caps log depth per saved query, so one busy card cannot bury the rest. |

---

## Troubleshooting

### `FAE_DB_BACKEND=neon requires DATABASE_URL to be set`

```
ValueError: FAE_DB_BACKEND=neon requires DATABASE_URL to be set. Set it in .env, or switch to FAE_DB_BACKEND=sqlite.

ERROR:    Application startup failed. Exiting.
```

The image ships `FAE_DB_BACKEND=neon`, so this is what a bare
`docker run fae` does. It is deliberate: the alternative is a silent fallback
to a SQLite file on an ephemeral filesystem that is wiped on the next deploy.

- Production or a real dev database: set `DATABASE_URL`.
- Just looking around: add `-e FAE_DB_BACKEND=sqlite`.

Check what the container received, without printing values:

```bash
docker run --rm fae env | grep -oE '^(FAE_[A-Z_]+|DATABASE_URL)=' | sort
```

### `/ready` returns 503 `SERVICE_NOT_READY`

```json
{"error_code":"SERVICE_NOT_READY","message":"The app-state database is not reachable.","detail":null}
```

`/ready` runs `SELECT 1` against the app-state database and the connection
failed. `/health` still returns 200, because the process is fine. The reason is
in the logs:

```bash
docker logs <container> 2>&1 | grep 'Readiness check failed'
```

Common causes, in the order worth checking:

1. The database is down or suspended. A serverless Postgres waking from
   suspend can exceed the readiness timeout on the first attempt and pass on
   the next.
2. Wrong host. See [the localhost entry](#connection-refused-to-localhost-from-inside-a-container).
3. Wrong credentials, or a URL mangled by `--env-file` quoting. See
   [the quoting entry](#a-value-from---env-file-arrives-with-its-quotes-attached).
4. The network path is gone: the database container was stopped, or the two
   containers are on different networks.

503 during the first minute after start is normal for a cold serverless
database. `fly.toml` gives readiness a 60s grace and the image's `HEALTHCHECK`
a 60s `start-period` for exactly this.

### `docker ps` says `unhealthy` but `/health` returns 200

```
NAMES     STATUS
fae-dev   Up About a minute (unhealthy)
```

Working as designed. The `HEALTHCHECK` polls `/ready`, not `/health`, because a
container that cannot reach its database cannot serve requests and should not
be sent traffic, even though its process is alive.

`/health` stays 200 on purpose. Wiring liveness to the database means a
database outage kills and reschedules processes that were working, turning a
dependency outage into a restart loop.

So `unhealthy` plus `/health` 200 reads as exactly one thing: **the process is
fine, its database is not**. Fix the database, not the container.

**Expect it to take about 100 seconds to flip.** `HEALTHCHECK` is
`--interval=30s --retries=3`, so three consecutive failures at 30-second
spacing have to land before Docker changes the status. Measured after stopping
the database under a running container: `docker ps` kept reporting `healthy`
for roughly a minute and a half. If you kill the database to try this and check
immediately, you are looking at a stale status, not a broken healthcheck.

To see why the check failed, in a readable form:

```bash
docker inspect --format '{{range .State.Health.Log}}exit={{.ExitCode}}
{{.Output}}
---
{{end}}' <container>
```

`{{json .State.Health}}` also works but prints a JSON-escaped multi-line
traceback on a single line, which is unreadable.

**The recorded `Output` is a Python traceback, never the 503 body.** The
healthcheck is `urllib.request.urlopen(...)`, which raises on a non-200 status
and never reads the response, so the JSON error envelope is not in the output.
Both failure shapes exit `1`:

```
urllib.error.HTTPError: HTTP Error 503: Service Unavailable    <- /ready answered
TimeoutError: timed out                                        <- the connect hung
```

The second is common when the database is gone entirely: the connection attempt
outlives the healthcheck's own 5s timeout, and Docker records
`Health check exceeded timeout (5s)` with exit `-1`. Both mean the same thing.

### `unable to open database file` on a bind mount

```
sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) unable to open database file
```

A bind mount keeps the host directory's ownership, and the process is uid
10001, so it cannot create `fraud_analyzer.db` in a directory owned by you. A
*named volume* does not have this problem: Docker seeds it from the image
directory it covers, so it inherits uid 10001 and mode 0700.

The working command, and both alternatives, are in
[2c](#2c-sqlite-on-a-bind-mount).

### `PermissionError: [Errno 13] Permission denied: '.secrets/fernet.key'`

You passed `--user` with a uid other than 10001 and did not set
`FAE_FERNET_KEY`. `/app/.secrets` is mode 0700 owned by uid 10001, so no other
uid can write the key file into it.

Set `FAE_FERNET_KEY` and nothing is ever written there. That is the fix, and it
is the right one anyway on anything but a throwaway container.

The same thing appears as
`OSError: [Errno 30] Read-only file system: '.secrets/fernet.key'` under
`--read-only`, with the same fix.

### A value from `--env-file` arrives with its quotes attached

Docker's `--env-file` parser is not a shell. It does not strip quotes, does not
expand variables, and does not process backslashes. Everything after the first
`=` is the literal value.

Given this file:

```
FAE_SQLITE_ALLOWED_DIRS=""
FAE_CORS_ORIGINS='https://dash.example.com'
```

the process sees:

```
raw sqlite_allowed_dirs = '""'
resolved allowlist      = [PosixPath('/app/""')]
raw cors_origins        = "'https://dash.example.com'"
cors_origin_list        = ["'https://dash.example.com'"]
```

Both are wrong and neither raises. An allowlist meant to be empty now contains a
directory literally named `""`. `FAE_CORS_ORIGINS` will never match a browser's
`Origin` header, so every dashboard request is blocked by CORS with nothing in
the logs explaining why.

Write env-file values bare:

```
FAE_SQLITE_ALLOWED_DIRS=
FAE_CORS_ORIGINS=https://dash.example.com
```

`-e` on the command line behaves the opposite way, because your shell strips
the quotes before Docker sees them, so `-e FAE_SQLITE_ALLOWED_DIRS=""` really
does set an empty string.

### `connection refused` to `localhost` from inside a container

```
sqlalchemy.exc.OperationalError: (psycopg.OperationalError) connection failed: connection to server at "127.0.0.1", port 5432 failed: Connection refused
	Is the server running on that host and accepting TCP/IP connections?
Multiple connection attempts failed. All failures were:
- host: 'localhost', port: 5432, hostaddr: '::1': connection failed: ...
- host: 'localhost', port: 5432, hostaddr: '127.0.0.1': connection failed: ...
```

`localhost` inside a container is that container's own loopback. Nothing is
listening on it.

| Where the database is | What to use as the host |
|---|---|
| Another compose service | The service name (`postgres`), resolved on the compose network |
| Another container you started by hand | Its container name, with both on the same user-defined network. The default bridge does not resolve names. |
| The host machine | `host.docker.internal` on Docker Desktop. On plain Linux, add `--add-host=host.docker.internal:host-gateway`, or use the host's LAN address. |
| Managed (Neon and similar) | Its real hostname, with `?sslmode=require`. |

Publishing the database's port with `-p 5432:5432` does not help: that exposes
it on the *host*, which is a different network namespace from the container
trying to reach it.

### The analyzer exits at startup right after `docker compose up`

If it failed connecting to Postgres, the `depends_on` gate is not doing its
job. It must be:

```yaml
depends_on:
  postgres:
    condition: service_healthy
```

Plain `depends_on: [postgres]` waits only for container creation. The analyzer
connects during startup, before serving, so it needs the database actually
accepting connections.

Doing it by hand instead? `pg_isready` polls once and returns; it is not a
wait. Run immediately after `docker run postgres:16` it prints
`/var/run/postgresql:5432 - no response` and exits 2. Loop on it:

```bash
until docker exec fae-pg pg_isready -U fae -d fae >/dev/null 2>&1; do sleep 1; done
```

### `docker compose up` uses a variable I never set

Compose auto-loads `.env` from the project directory for variable substitution,
and `services/analyzer/.env` is the **application's** settings file. Verified:
with `FAE_FERNET_KEY` unset in the shell, compose still resolved it from that
file and the `:?` guard never fired.

To see what compose resolves without it:

```bash
docker compose --env-file /dev/null config
```

Never write `${DATABASE_URL}` into `docker-compose.yml`: it would substitute
whatever is in `.env`, which on a configured machine is a production database.

### `port is already allocated`, and then `the name is in use`

Port 8000 is contended: another dev server, another copy of this stack, or a
previous run that is still up. It double-faults, and the second error hides the
first.

**First attempt**, the real problem:

```
docker: Error response from daemon: driver failed programming external connectivity
on endpoint fae-look (...): failed to bind port 127.0.0.1:8000/tcp:
Error starting userland proxy: listen tcp4 127.0.0.1:8000: bind: address already in use.
```

The container was **created** before the bind failed, so it is still there:

```
$ docker ps -a --filter name=fae-look
NAMES      STATUS
fae-look   Created
```

**Second attempt**, after freeing the port and rerunning the same command:

```
docker: Error response from daemon: Conflict. The container name "/fae-look" is
already in use by container "14e01898b5be...". You have to remove (or rename)
that container to be able to reuse that name.
```

Which looks like a different problem and is not. Remove the corpse first:

```bash
docker rm fae-look          # no -f needed, it never started
docker run -d --name fae-look ... fae
```

Or skip the whole thing by publishing somewhere else. On compose that is one
variable:

```bash
FAE_HOST_PORT=8080 docker compose up -d
```

To find what holds the port: `ss -ltnp | grep 8000`, or
`docker ps --format '{{.Names}}\t{{.Ports}}' | grep 8000`.

### The container is killed with exit 137 and no error in the log

Out of memory. The kernel's OOM killer leaves nothing in the application log
because the process gets no chance to write one.

```bash
docker inspect --format 'OOMKilled={{.State.OOMKilled}} exit={{.State.ExitCode}}' <container>
```

Raise `--memory`, or lower `FAE_CACHE_MAX_BYTES` (64 MB default) and
`FAE_MAX_RESULT_BYTES` (32 MB default). The arithmetic is in
[3. Production](#3-production).

Exit 137 also appears for a different reason: a `docker stop` whose 10-second
grace elapsed. `OOMKilled` is what distinguishes them. See
[Stopping and draining](#stopping-and-draining).

### Printing the environment prints secrets

`docker exec <c> env` writes `FAE_FERNET_KEY` and the full `DATABASE_URL`,
password included, to your terminal and your shell history. The startup banner
goes to the trouble of redacting that password; do not undo it.

Names only:

```bash
docker exec <c> env | grep -oE '^(FAE_[A-Z_]+|DATABASE_URL)=' | sort
```

Non-secret values, plus a length check for the rest:

```bash
docker exec <c> sh -c 'echo "$FAE_CORS_ORIGINS"; echo "$FAE_LOG_JSON"; echo "key is ${#FAE_FERNET_KEY} chars"'
```

### The startup banner is missing from the logs

Three causes, in the order they actually happen:

**1. It has not booted yet.** This is the common one. The banner is written
after Alembic connects, roughly 8 seconds into a normal start and longer
against a cold serverless database. A `docker run` followed immediately by
`docker logs | grep` finds nothing because nothing has been written. Wait for
the port instead of guessing:

```bash
until curl -sf localhost:8000/health >/dev/null; do sleep 1; done
docker logs <container> 2>&1 | grep 'App-state backend'
```

`docker ps` showing `(health: starting)` rather than `(healthy)` says the same
thing.

**2. `FAE_LOG_LEVEL` is above `INFO`.** The banner, the migration lines, and
the service's access log are all `INFO`. Set `FAE_LOG_LEVEL=INFO`.

**3. The process died before `bootstrap_schema()` ran.** `docker ps -a` shows
it exited; read the traceback at the end of `docker logs`.

### Editing a doc triggers a full dependency reinstall

It should not. `.dockerignore` excludes `**/*.md`, so Markdown is not in the
build context and cannot invalidate the layer that runs
`uv sync --frozen --no-dev --no-install-project`.

If a rebuild does re-resolve dependencies, something changed `pyproject.toml`
or `uv.lock`, which is the layer above. `docker build --progress=plain .` shows
which step missed the cache.

---

## Cleaning up

Compose:

```bash
cd services/analyzer
docker compose down          # containers and network, database kept
docker compose down -v       # also deletes the pgdata volume. Not reversible.
```

Hand-run containers:

```bash
docker rm -f fae fae-dev fae-local fae-look fae-pg
docker network rm fae-net
docker volume rm fae-data          # deletes the SQLite app-state database
docker image rm fae
```

The bind-mount directory from [2c](#2c-sqlite-on-a-bind-mount). It is untracked
and not gitignored, so leaving it behind dirties `git status`:

```bash
rm -rf ./data
```

If you took the `sudo chown 10001:10001 ./data` route, that directory is no
longer yours and the line above fails with `Permission denied`. Removing it
needs root, so it is yours to run:

```bash
# Run this yourself; it needs sudo.
sudo rm -rf ./data
```

`docker volume rm` and `docker compose down -v` are not reversible. They take
every saved connection, saved query, dashboard, and execution log with them.
