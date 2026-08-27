# Fraud Analyzer Engine

A schema-agnostic backend. Point it at a Postgres, MySQL, or SQLite database,
save named SQL queries against it, and every saved query becomes a chart-ready
JSON endpoint a dashboard can poll.

There is no fraud-detection logic in this service, by design. The fraud logic
is the SQL you write and save. What this service provides is safe connectivity,
safe execution, and clean chart-shaped output against a schema it knows nothing
about in advance.

## Use a read-only database role

**Create a read-only role for every connection you add.** This is the single
most important thing an operator does with this service.

The application blocks writes at three layers: the SQL guard rejects anything
that is not a SELECT, the driver connects in read-only mode, and a statement
timeout bounds every query. All three are defence in depth. None of them is the
backstop. The backstop is a database role with no rights to write, because that
is the only control that still holds if this application has a bug.

```sql
-- PostgreSQL
CREATE ROLE fraud_ro LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE yourdb TO fraud_ro;
GRANT USAGE ON SCHEMA public TO fraud_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO fraud_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO fraud_ro;

-- MySQL
CREATE USER 'fraud_ro'@'%' IDENTIFIED BY '...';
GRANT SELECT ON yourdb.* TO 'fraud_ro'@'%';

-- SQLite: point sqlite_path at a file the service user can read but not write.
```

## Where saved data lives

There are two kinds of database in this system, and it is worth keeping them
straight.

- The **app-state database** stores connection profiles, saved queries, and
  execution logs. This is the service's own storage, and it is what a frontend
  reads saved queries back from.
- A **target database** is a customer database you point the service at to run
  fraud SQL against. The service only ever reads from these.

`FAE_DB_BACKEND` selects the app-state store:

| Value | Store | Uses |
|---|---|---|
| `sqlite` (default) | a local file | `FAE_SQLITE_APP_DB_PATH`, default `./fraud_analyzer.db` |
| `neon` | managed Postgres | `DATABASE_URL` |

```bash
# local development, zero setup
FAE_DB_BACKEND=sqlite

# shared Postgres a frontend can also read
FAE_DB_BACKEND=neon
DATABASE_URL=postgresql://user:pass@host/dbname?sslmode=require
```

`DATABASE_URL` is read under that exact name, since that is what every managed
Postgres host injects, and `FAE_DATABASE_URL` works too. A `postgresql://` or
`postgres://` scheme is rewritten to `postgresql+psycopg://` automatically, so
a connection string can be pasted in unedited.

Selecting `neon` without a `DATABASE_URL` fails at startup rather than falling
back to SQLite. A silent fallback would write saved queries to a local file
nobody would think to look in.

`FAE_APP_DB_URL` is a full escape hatch: set it and it is used verbatim,
ignoring `FAE_DB_BACKEND`. The test suite uses it to give each test its own
throwaway database.

Run the migrations against whichever backend is selected:

```bash
FAE_DB_BACKEND=neon uv run alembic upgrade head
```

### Enum columns store values, not names

Columns like `db_type`, `status`, and `chart_type` hold the same lowercase
strings the JSON API uses: `sqlite`, `ok`, `line`. SQLAlchemy's default is to
store the enum *member name* (`SQLITE`, `OK`, `LINE`), which caused two
problems worth knowing about if you query the database directly:

- Every `server_default` is written as a value, so a row created by a
  migration, a seed script, or any client writing SQL directly was unreadable
  by the service. It raised `LookupError: 'sqlite' is not among the defined
  enum values`.
- A frontend reading the database saw different strings than the API returned
  for the same field.

Migration `0002_enum_values` rewrites existing rows to the value spelling and
is idempotent.

### Serverless cold starts

Neon suspends an idle database, and the first connection afterwards can take
well over ten seconds. `FAE_APP_DB_CONNECT_TIMEOUT_S` defaults to 30 for that
reason, separately from `FAE_CONNECT_TIMEOUT_S`, which bounds connections to
target databases where a slow connect means something is wrong. The app-state
pool also recycles every 300s, since a managed host drops idle connections.

## Deploying to a container

**[`DOCKER.md`](DOCKER.md) is the full guide**: local, development, and
production, across both app-state backends, with a configuration reference
generated from `app/config.py` and a troubleshooting section built from
reproduced failures. What follows is the short version.

`Dockerfile` and `fly.toml` both live in `services/analyzer/`, and every
container command runs from there. That is not cosmetic: a Docker build context
and a Fly build context are both "the directory the config sits in", and the
build needs `pyproject.toml` and `uv.lock`, which are here and not at the repo
root. With `fly.toml` at the root, `COPY pyproject.toml uv.lock ./` had nothing
to copy and `fly deploy` could not build at all.

```bash
cd services/analyzer
docker build -t fae .
fly deploy
```

The image runs as uid 10001, installs from the lockfile in a build stage so no
compiler or `uv` ships in the runtime layer, and keeps the code and virtualenv
root-owned so a service whose whole job is running caller-supplied SQL cannot
rewrite its own source. It defaults `FAE_DB_BACKEND=neon` and
`FAE_SQLITE_ALLOWED_DIRS=""` because neither SQLite app-state nor a SQLite
target can work on an ephemeral container filesystem, and it writes to exactly
two paths: `/app/.secrets` (only when `FAE_FERNET_KEY` is unset) and
`/app/data` (only when the app-state backend resolves to SQLite).

Three environment variables decide whether a deployment works, and getting any
of them wrong surfaces later as missing tables, blocked dashboard requests, or
unreadable credentials rather than at deploy time: `DATABASE_URL`,
`FAE_FERNET_KEY`, and `FAE_CORS_ORIGINS`. `DOCKER.md` covers each one, what its
absence actually costs, and how to confirm from the outside that it took.

**This service has no authentication and serves an interactive SQL console at
`/docs` on the same port.** Publish it on loopback (`-p 127.0.0.1:8000:8000`)
behind an authenticating, TLS-terminating reverse proxy. `FAE_CORS_ORIGINS`
constrains browsers only and stops no direct caller; rate limits are
containment, not auth. See
[`DOCKER.md`](DOCKER.md#before-you-expose-this).

### Migrations run at startup

`FAE_AUTO_MIGRATE` defaults to `true`, so the service brings its own schema up
to head before serving. A container deploy has no shell step between building
the image and starting the server, and a service that cannot create its own
schema answers every request with `no such table: connections`.

Set `FAE_AUTO_MIGRATE=false` if you run migrations as a separate release step,
or if several instances start at once and you would rather they not race.

`bootstrap_schema()` calls `verify_schema()` unconditionally
(`app/db/migrate.py:145`), whether or not it migrated. With auto-migrate on
that confirms the upgrade actually produced the tables; with it off it is the
gate that refuses to serve against an unmigrated database, naming the command
to run.

### Reading the startup log

```
App-state backend=neon (from FAE_DB_BACKEND) url=postgresql+psycopg://user:***@host/dbname auto_migrate=True
Applying app-state migrations to postgresql+psycopg://user:***@host/dbname
```

The password is redacted. If that first line says `backend=sqlite` on a
container, the deployment is not configured and its data will not survive.

The line names which setting won, because `FAE_APP_DB_URL` overrides
`FAE_DB_BACKEND` entirely and a banner reading `backend=neon` beside a
`sqlite://` URL is exactly the confusion this line exists to prevent.

Nothing in the service configured the root logger until recently, so uvicorn's
default left root at `WARNING` and this line was discarded: it was documented
and unreachable. `FAE_LOG_LEVEL` (default `INFO`) now controls it.

`FAE_LOG_JSON=true` switches **the service's own logger** to one JSON object
per line. uvicorn has its own logging configuration and is unaffected, so its
four startup lines and its access log stay plain text on the same stream. A log
shipper has to tolerate both. [`DOCKER.md`](DOCKER.md#what-the-logs-actually-look-like)
has the measured breakdown and how to drop uvicorn's duplicate access line.

Every response carries an `X-Request-ID` header, echoed from the request when
supplied, and every log line emitted while serving that request carries the
same id.

### Health and readiness

`/health` is liveness and touches nothing: a liveness probe that fails during a
database outage makes the orchestrator kill and reschedule a process that was
working, turning a dependency outage into a restart loop.

`/ready` is readiness and runs `SELECT 1` against the app-state database,
returning 503 `SERVICE_NOT_READY` when it cannot. Give it a generous grace
period. A suspended Neon instance can take over ten seconds just to accept a
connection, and startup runs migrations before serving, so a short grace kills
the machine mid-migration. `services/analyzer/fly.toml` sets 60s.

## Setup

Requires [uv](https://docs.astral.sh/uv/). Python 3.12+.

```bash
cd services/analyzer
uv venv --python 3.13
uv pip install -e ".[dev]"

cp .env.example .env
# Generate a Fernet key and put it in .env as FAE_FERNET_KEY:
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

Interactive docs at http://127.0.0.1:8000/docs. Run the tests with
`uv run pytest`.

## Accounts

Every endpoint except `/health`, `/ready`, `/auth/login`, and `/auth/logout` needs a signed-in user.

FastAPI's own documentation paths - `/docs`, `/docs/oauth2-redirect`, `/redoc`
and `/openapi.json` - also answer without one. That is a deliberate, recorded
decision rather than an oversight: the OpenAPI schema describes the API's
shape and carries no rows, credentials or SQL, and the engine sits behind a
private network with Next.js as its only public face. To switch them off in a
given deployment, construct the app with `FastAPI(docs_url=None,
redoc_url=None, openapi_url=None)` in `app/main.py`. They are listed in
`PUBLIC_PATHS` (`app/security/deps.py`) and `tests/test_route_coverage.py`
fails the build if any other unauthenticated route appears beside them.

Create the first administrator. This is the only way an admin account comes
into existence: it is a CLI command, not an HTTP endpoint, because no admin
exists yet to authenticate a request that could create one. It needs shell
access to the machine running the app rather than network access to it.

    cd services/analyzer
    uv run fae create-admin

Under Docker:

    docker compose exec analyzer fae create-admin

The command prints the database it is about to write to. If that is not the
database you expect, you are in the wrong directory: the settings are read from
`services/analyzer/.env`.

Other commands:

    uv run fae reset-password --email someone@example.com
    uv run fae list-users

`reset-password` prints a temporary password once. It is valid for 72 hours and
the holder must choose a new one before they can use anything else.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/connections` | Create a profile and test it immediately |
| GET | `/connections` | List profiles, never with credentials |
| GET | `/connections/{id}` | One profile |
| PUT | `/connections/{id}` | Partial update, then re-test |
| POST | `/connections/{id}/test` | Re-test, updating status |
| DELETE | `/connections/{id}` | Delete, cascading to saved queries |
| GET | `/connections/{id}/tables` | Tables and views |
| GET | `/connections/{id}/tables/{table}/columns` | Columns and types |
| POST | `/connections/{id}/queries` | Save a query, validated and dry-run |
| GET | `/connections/{id}/queries` | List a connection's queries |
| POST | `/connections/{id}/query/preview` | Run ad-hoc SQL without saving |
| GET/PUT/DELETE | `/queries/{id}` | Read, update, delete a saved query |
| POST | `/queries/{id}/run` | Execute now, always fresh |
| GET | `/queries/{id}/poll` | Cheap change check for an interval loop |
| POST | `/queries/poll` | Poll up to 100 queries in one request |
| GET | `/queries?ids=a,b,c` | Resolve many saved queries in one request |
| GET | `/queries/{id}/logs` | Recent execution attempts |
| GET | `/health` | Liveness; touches nothing |
| GET | `/ready` | Readiness; checks the app-state database |

The two batch endpoints exist because a dashboard is the normal case. Twelve
cards previously cost thirteen requests to paint and twelve more every five
seconds, each taking a worker thread, an app-state session, and on a cache miss
a target connection.

Response shapes and the full `error_code` table are in
[`contracts/analyzer-api.md`](../../contracts/analyzer-api.md).

## How the safety layer works

Every statement, saved or ad-hoc, goes through `app/security/sql_guard.py`
before any target database sees it. The order of checks is load-bearing.

**1. Comments are stripped first, and the stripped text is what executes.**
This is what defeats statement stacking hidden behind a comment:

```sql
SELECT 1 --
; DROP TABLE users
```

Split the raw text and you see one statement, because the `;` looks commented
out. The database sees two. Stripping before splitting removes the disguise. It
also destroys MySQL's executable `/*! ... */` comments, whose body MySQL runs
and every other parser treats as inert.

**2. Exactly one statement.** More than one is rejected.

**3. The statement type must be SELECT.** `sqlparse` resolves a CTE to the
command that follows it, so `WITH x AS (...) DELETE FROM t` reports as `DELETE`
and is rejected here. This one check also rejects `PRAGMA`, `ATTACH`, `COPY`,
`LOAD DATA`, `SET`, `USE`, `CALL`, `EXPLAIN`, `TABLE`, and `VALUES`.

**4. A keyword blocklist**, for writes hidden inside something that genuinely
does report as a SELECT:

```sql
WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x
```

The blocklist is deliberately short and holds only ANSI-reserved words.
`sqlparse` tags plenty of non-reserved words as keywords even when they are
ordinary column names, so a blanket keyword scan would reject
`SELECT comment, load, copy FROM audit_notes`, which is valid SQL. Reserved
words cannot appear unquoted as an identifier, and a quoted identifier
tokenises as a name rather than a keyword. Command words are caught by check 3
instead, by position.

Blocking `UPDATE` also blocks `SELECT ... FOR UPDATE`. That is intended: it is a
locking read with no place in an analytics query.

**5. A function blocklist.** A statement can be a flawless SELECT and still read
the filesystem or open a network connection:

```sql
SELECT pg_read_file('/etc/passwd')
SELECT load_file('/etc/passwd')          -- MySQL
SELECT readfile('/etc/passwd')           -- SQLite
SELECT dblink('host=evil', 'SELECT 1')
SELECT pg_sleep(300)                     -- denial of service
```

Matching is on the call shape, a name followed by `(`, so a column that happens
to be named `sleep` still works.

**Quoted names count, and getting this wrong voided the whole list.** `sqlparse`
types a double-quoted identifier as `String.Symbol`, not `Name`. An earlier
version scanned only name and keyword tokens, so this walked straight past the
blocklist while the bare form was correctly rejected:

```sql
SELECT "pg_read_file"('/etc/passwd')     -- PostgreSQL accepts both spellings
```

Two quote characters defeated every entry. The corpus now pins each blocklisted
function in bare, double-quoted, backticked, and bracketed form.

The list also covers functions that write or change session state, not just
ones that read files. The important one is `set_config`:

```sql
SELECT set_config('default_transaction_read_only', 'off', false)
```

That turns off the *second* layer of the read-only guarantee (see below) for
the life of the pooled connection, so it would survive into later requests
reusing the same handle. That is a privilege escalation, not a coverage gap.
`nextval`, `setval`, `pg_terminate_backend`, `pg_notify`, `get_lock` and the
large-object and `dblink` families are blocked for the same reason.

`generate_series` and `repeat` are deliberately **not** blocked. Both are
ordinary in analytics, and both are bounded by the statement timeout and the
result byte budget instead.

**6. Locking reads.** `FOR SHARE` and `FOR KEY SHARE` take row locks on the
customer's production tables. They cannot go on the keyword blocklist, because
`sqlparse` types a bare `share` as a keyword too and blocking the word would
reject `SELECT share FROM positions`. The check anchors on the `FOR ... SHARE`
sequence, which is what separates the clause from the column.

**Statement length is capped** at `FAE_MAX_SQL_LENGTH`, checked before anything
parses. `sqlparse` is pure Python and superlinear in token density, so an
unbounded statement is a CPU denial of service that never reaches a database
and is therefore never bounded by the statement timeout. A 1.6MB statement
measured 51 seconds. Accepted statements are memoised, so a saved query pays
parsing once rather than on every poll.

The adversarial corpus in `tests/test_sql_guard.py` is the quality gate for this
component. A single bypass there means the fraud tool is itself an attack
surface.

## Row caps are enforced on the fetch side

The obvious approach, wrapping the user's SQL as
`SELECT * FROM (<sql>) t LIMIT n`, is not used. It breaks on MySQL whenever the
inner query has duplicate output column names, and appending `LIMIT` textually
is fragile against `UNION`, an existing `LIMIT`, and window or CTE forms.

Instead the service fetches `row_limit + 1` rows and stops. Getting that extra
row back is the truncation signal, reported as `"truncated": true`. One code
path for every engine, and no query shape can defeat it. Server-side work stays
bounded by the statement timeout, which is what actually protects the target.

## Read-only enforcement per driver

| Database | Read-only | Timeout |
|---|---|---|
| PostgreSQL | `default_transaction_read_only=on` as a connection option | `statement_timeout` |
| MySQL | `SET SESSION TRANSACTION READ ONLY` on every pooled connection | `max_execution_time` plus socket `read_timeout` |
| SQLite | opened with the `mode=ro` URI flag | progress handler aborting past a deadline |

PostgreSQL gets the setting as a connection option rather than a per-transaction
`SET TRANSACTION READ ONLY`, so it cannot be forgotten on some code path. SQLite
has no server-side statement timeout at all, so the only way to bound a runaway
query is the progress handler, which SQLite calls every N virtual-machine
instructions.

## Polling

`GET /queries/{id}/poll?since_hash=<last hash>` is what a dashboard's interval
loop calls. Each response carries `poll_interval_ms`, so the frontend does not
have to guess the cadence.

Results are cached per query for `poll_interval_ms`. Inside that window a poll
compares hashes in memory and never opens a connection to the target database.
Without the cache, one chart on a five-second interval would run 720 real
queries per hour against a customer's production database.

`POST /queries/{id}/run` always bypasses the cache, and editing a query
invalidates it, so nobody is served rows from SQL they just replaced. Pass
`force=true` to `/poll` for a guaranteed-fresh read.

`data_hash` is a sha256 over the exact payload the client receives. Values are
coerced to JSON-safe types once, before hashing, so the hash cannot disagree
with the body. `Decimal` serialises as a string rather than a float, so a value
with no exact binary floating-point representation cannot hash differently on
different platforms.

## Deleting a connection cascades

`DELETE /connections/{id}` deletes the connection, every saved query on it, and
every execution log row for those queries. This is a single-tenant tool, so
there is no other user whose saved work a delete could destroy. Move queries to
another connection first if you want to keep them.

## Credentials

Passwords are encrypted at rest with Fernet and decrypted only in-process, when
a connection URL is built. No plaintext password is ever persisted.

No response model in this API declares a `password` or `password_encrypted`
field. A credential cannot leak through a response model that has nowhere to
put it, which is a stronger guarantee than remembering to strip secrets at each
call site. `tests/test_connections_api.py` asserts the plaintext appears in no
response body.

## Configuration

Every setting is an environment variable prefixed `FAE_`, with a working local
default. See `.env.example` for the full list.

One worth knowing: `POST /connections` tests the connection as part of creating
it, so creating a connection to an unreachable host blocks for up to
`FAE_CONNECT_TIMEOUT_S`. Lower it if that latency matters to your UI.

### Limits worth understanding

| Setting | Default | Why it exists |
|---|---|---|
| `FAE_SQLITE_ALLOWED_DIRS` | `.` | Directories a SQLite **target** may point into. See below; empty disables SQLite targets entirely, which is right for a container. |
| `FAE_MAX_SQL_LENGTH` | `8000` | Bounds how much CPU one statement can spend in the parser. |
| `FAE_MAX_RESULT_BYTES` | `32MB` | Refuses a runaway payload while rows are read. `row_limit` bounds row count, not row width. |
| `FAE_CACHE_MAX_BYTES` | `64MB` | Poll cache budget. It used to be bounded by entry count, which said nothing about cost. |
| `FAE_RATE_LIMIT_PER_MINUTE` | `600` | General per-IP budget. 0 disables. |
| `FAE_RATE_LIMIT_EXECUTION_PER_MINUTE` | `300` | Budget for anything opening a target connection. |
| `FAE_MAX_REQUEST_BYTES` | `1MB` | Refuses an oversized body before it is read. |
| `FAE_LOG_RETENTION_DAYS` | `30` | Execution logs are pruned at startup past this age. 0 disables. |
| `FAE_MAX_LOGS_PER_QUERY` | `1000` | Caps log depth per query, so one busy card cannot bury the rest. |

### SQLite target connections are restricted by path

A connection profile carries a filesystem path that the API process opens
directly. Unconstrained, that is not a database connection, it is an
arbitrary-file-read primitive, and its best target is this service's own
app-state database: point a connection at it and
`SELECT name, password_encrypted FROM connections` returns every stored
credential through the ordinary query endpoints.

So a SQLite target must resolve inside `FAE_SQLITE_ALLOWED_DIRS`, and the
app-state database is refused whatever that allows. Paths are compared after
resolving symlinks and `..`, and the check runs when the connection is *used*,
not only when it is created, so a row written before the allowlist existed is
still checked.

This is containment, not authentication. It does not decide who may register a
connection; it bounds the damage of the ones they can.

**A SQLite target cannot work in a container at all.** The path is resolved
against the container's filesystem, so a path from a developer's laptop
resolves to nothing, and a Fly volume attaches to one machine so it would work
on some requests and fail on others. Use `postgres` or `mysql` with a read-only
role for anything deployed. The Dockerfile and `services/analyzer/fly.toml` set
`FAE_SQLITE_ALLOWED_DIRS=""` so this fails with a clear message rather than a
confusing one.

### Rate limiting

There is no authentication, by scope. That makes the per-IP budget the only
thing between the open internet and an endpoint that runs caller-supplied SQL
against a production database. Buckets are fixed one-minute windows held per
process, so behind several instances the effective limit multiplies by
instance count. `X-Forwarded-For` is honoured (Fly's proxy makes every request
appear to come from one peer otherwise); it is client-controlled and therefore
spoofable, which is another reason this is containment rather than auth.

## Tests

```bash
uv run pytest                              # everything
uv run pytest -m "not integration"         # the commit gate lane
uv run pytest tests/test_sql_guard.py -v   # the adversarial corpus
uv run pytest tests/test_live_targets.py   # real Postgres and MySQL
```

Two lanes. The **gate lane** (`-m "not integration"`) is 168 tests that need no
database, HTTP client, or migration; it runs on every commit via the hook in
`scripts/hooks/pre-commit`, installed with `./scripts/install-hooks.sh`. The
**integration lane** stands up SQLite databases, a `TestClient`, and Alembic.

Test files join a lane by filename, in `tests/conftest.py`, so a new file lands
in the right one without anyone remembering to mark it.

On the "gate tests must finish in under 2s" convention: that is not reachable
here, and the numbers say why. Importing `app.main` costs 2.6s on its own
(FastAPI plus SQLAlchemy plus Pydantic) and pytest startup costs 1.2s, so
roughly 3.8s is fixed before a single assertion runs. The 168 gate tests
themselves take about 3s. Total is ~7s, and the only way materially below that
would be to stop importing the application.

### Live database tests

SQLite cannot exercise the code paths that protect a production database:
`default_transaction_read_only`, `statement_timeout`, `SET SESSION TRANSACTION
READ ONLY`, `max_execution_time`, and SQLSTATE-based error mapping.
`tests/test_live_targets.py` covers those against real servers and skips itself
automatically when none are reachable, so the default suite stays hermetic.

```bash
docker run -d --name fae-pg -e POSTGRES_PASSWORD=rootpw -e POSTGRES_DB=fraud \
    -p 55432:5432 postgres:16-alpine
docker run -d --name fae-my -e MYSQL_ROOT_PASSWORD=rootpw -e MYSQL_DATABASE=fraud \
    -p 53306:3306 mysql:8
# then seed both from tests/fixtures/live_seed.sql
```

The application user in that fixture is granted **full write access on
purpose**. It means the only thing preventing a write is this service's own
enforcement, not the database role. A test that relied on a read-only role
would prove nothing about this code.

One production bug these tests caught: pymysql's socket `read_timeout` was set
equal to the server's `max_execution_time`, so the two fired together and the
socket usually won. A merely slow query surfaced as errno 2013 "lost
connection", mapping to `502 DB_UNREACHABLE`, which tells a frontend the
database is down when it is not. The socket deadline is now
`FAE_QUERY_TIMEOUT_S + FAE_SOCKET_TIMEOUT_GRACE_S`, so the server always gets
to return its own `504 QUERY_TIMEOUT` first.

`filterwarnings` turns deprecation warnings from `app.*` into errors, so this
codebase cannot quietly accumulate deprecated API usage.

**On evals:** the project convention is that every feature ships with a test
suite and an eval suite. This service contains no LLM, so there is nothing an
eval could measure. The adversarial SQL corpus is the equivalent gate: a fixed
set of attack inputs with a hard pass threshold of zero bypasses. Writing an
"eval suite" here would be ceremony, not measurement.

## Layout

```
services/analyzer/
├── app/
│   ├── main.py                  FastAPI app, CORS, exception handlers
│   ├── config.py                env-var settings
│   ├── errors.py                ErrorCode enum, single code -> HTTP mapping
│   ├── db/
│   │   ├── app_state.py         this service's own database
│   │   └── target_registry.py   pooled read-only engines per connection
│   ├── models/                  app-state SQLAlchemy models
│   ├── schemas/                 Pydantic request/response models
│   ├── security/
│   │   ├── sql_guard.py         the SELECT-only validator
│   │   └── crypto.py            Fernet credential encryption
│   ├── routers/                 connections, introspection, queries
│   └── services/                business logic, result cache
├── alembic/                     app-state migrations
└── tests/
```
