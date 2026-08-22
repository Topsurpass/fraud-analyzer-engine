# Fraud Analyzer Engine - Design

Date: 2026-08-22
Status: Approved

## 1. Purpose

A schema-agnostic FastAPI backend. A user connects to arbitrary databases
(Postgres, MySQL, SQLite), saves named SQL queries against those connections,
and exposes each saved query as a chart-ready JSON endpoint the frontend polls.

Backend only. No frontend code. No built-in fraud-detection logic: fraud logic
lives entirely in the SQL the user writes. The engine provides safe
connectivity, safe execution, and chart-shaped output.

Two properties gate everything else, in priority order:

1. Saved and ad-hoc SQL can only read. If the query layer can write, alter, or
   exfiltrate beyond a SELECT, the tool is the risk.
2. Behaviour is identical regardless of the target schema. No hardcoded table
   or column assumptions anywhere in core logic.

## 2. Measurable outcome

- Every statement in the adversarial corpus (`tests/test_sql_guard.py`) is
  rejected with a specific `error_code`. Zero bypasses.
- A saved query against a previously unseen SQLite/Postgres/MySQL schema
  returns a populated `columns`/`rows` payload with no code change.
- A `/poll` call against a warm cache issues zero target-DB queries, verified
  by a query counter in the integration test.

## 3. Layout

Services-first per CLAUDE.md. One service today, room for siblings.

```
fraud-analyzer-engine/
├── services/analyzer/
│   ├── app/
│   │   ├── main.py                 FastAPI app, CORS, exception handlers
│   │   ├── config.py               env-var settings
│   │   ├── errors.py               ErrorCode enum + AppError hierarchy
│   │   ├── db/
│   │   │   ├── app_state.py        engine/session for the app's own DB
│   │   │   └── target_registry.py  pooled engines per Connection id
│   │   ├── models/                 SQLAlchemy models for app-state DB
│   │   ├── schemas/                Pydantic v2 request/response models
│   │   ├── security/
│   │   │   ├── sql_guard.py        SELECT-only validator
│   │   │   └── crypto.py           Fernet encrypt/decrypt
│   │   ├── routers/
│   │   │   ├── connections.py
│   │   │   ├── introspection.py
│   │   │   └── queries.py
│   │   └── services/
│   │       ├── connection_service.py
│   │       ├── query_service.py
│   │       └── result_cache.py
│   ├── alembic/
│   ├── tests/
│   ├── pyproject.toml
│   └── README.md
├── contracts/analyzer-api.md       frozen response shapes + error_code table
├── scripts/export_openapi.py
└── README.md
```

Python 3.12 pinned via `uv`. System Python is 3.8.10; Pydantic v2, SQLAlchemy
2.0, and psycopg3 want newer.

## 4. Data model (app-state DB)

### Connection
| field | type | notes |
|---|---|---|
| id | UUID str PK | |
| name | str, unique | user-facing label |
| db_type | enum | postgres / mysql / sqlite |
| host, port, database, username | nullable | unused for sqlite |
| password_encrypted | str, nullable | Fernet, never returned by the API |
| sqlite_path | str, nullable | sqlite only |
| status | enum | untested / ok / failed |
| last_tested_at | datetime, nullable | |
| last_test_error | str, nullable | |
| created_at, updated_at | datetime | |

### SavedQuery
| field | type | notes |
|---|---|---|
| id | UUID str PK | |
| connection_id | FK -> Connection, ON DELETE CASCADE | |
| name | str | unique per connection |
| description | str, nullable | |
| sql_text | text | validated SELECT-only at save time |
| table_hint | str, nullable | UI display only |
| chart_type | enum | line / bar / pie / number / table |
| x_field, y_field, series_field | str, nullable | column names |
| row_limit | int | default 1000, hard ceiling 10000 |
| poll_interval_ms | int, nullable | falls back to global default |
| created_at, updated_at | datetime | |

### QueryExecutionLog
id, query_id (FK, cascade), executed_at, row_count, duration_ms, success,
error_code, error_message. Included, not deferred.

Plaintext passwords are never stored. No credential field appears in any API
response, enforced by response models that do not declare those fields.

## 5. SQL safety layer

One validator gates every statement, saved or ad-hoc. Order is load-bearing.

1. `sqlparse.format(sql, strip_comments=True)`. The stripped text is what gets
   executed. Stripping before splitting is what defeats
   `SELECT 1 --\n; DROP TABLE x`.
2. `sqlparse.split()` on the stripped text. More than one non-empty statement
   is rejected as `MULTIPLE_STATEMENTS`.
3. Statement type must be `SELECT`, or `WITH` whose CTE resolves to a SELECT.
   Anything else is `NON_SELECT_STATEMENT`.
4. Token-level keyword blocklist over `stmt.flatten()`, matching only tokens
   typed as keywords: INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE,
   GRANT, REVOKE, ATTACH, DETACH, PRAGMA, EXEC, EXECUTE, CALL, MERGE, REPLACE,
   COPY, LOAD, VACUUM, REINDEX, SET, LOCK, UNLOCK, USE, HANDLER, DO, INTO.
   Exact match against keyword tokens means a string literal `'DROP'` and a
   column named `updated_at` do not false-positive. Rejection is
   `FORBIDDEN_KEYWORD`.
5. Function blocklist, beyond the original brief. A pure SELECT can still
   exfiltrate: `pg_read_file`, `pg_read_binary_file`, `pg_ls_dir`, `lo_import`,
   `lo_export`, `dblink`, `dblink_exec`, `query_to_xml` (Postgres);
   `load_file` (MySQL); `readfile`, `writefile`, `load_extension` (SQLite).
   Rejection is `FORBIDDEN_FUNCTION`.

Every rejection returns HTTP 400 with a distinct `error_code` and a human
message naming the offending token, so the frontend can branch.

### Row cap: fetch-side, not SQL rewrite

The brief said wrap or append a LIMIT. Rejected in favour of a fetch-side cap.
`SELECT * FROM (<user sql>) t LIMIT n` breaks on MySQL when the inner query has
duplicate output column names, and appending LIMIT is fragile against UNION,
an existing LIMIT, and window/CTE forms.

Instead: `result.fetchmany(row_limit + 1)`. If row n+1 arrives, the payload is
truncated and the response carries `"truncated": true`. No SQL rewriting, one
code path for all three engines, and no query shape can defeat it. Server-side
work is bounded by the statement timeout, not by the cap.

### Read-only execution per driver

| DB | Read-only | Timeout |
|---|---|---|
| Postgres | connect_args `options=-c default_transaction_read_only=on` | `-c statement_timeout=<ms>` |
| MySQL | `SET SESSION TRANSACTION READ ONLY` on connect | `SET SESSION max_execution_time` plus socket `read_timeout` |
| SQLite | `file:<path>?mode=ro` URI creator | `set_progress_handler` that aborts past a deadline |

The README documents, under its own heading, that operators should supply a
read-only DB role for every connection. The app enforces read-only at the query
layer; a read-only DB user is the real backstop.

## 6. Endpoints

### Connections
- `POST /connections` create and immediately test. On failure still persist,
  with `status=failed`, and return the error alongside the created record.
- `GET /connections`, `GET /connections/{id}` never include credentials.
- `POST /connections/{id}/test` re-test, updates status and last_tested_at.
- `DELETE /connections/{id}` cascade-deletes saved queries and execution logs.
  Documented in the README and the endpoint docstring.

### Introspection
- `GET /connections/{id}/tables` via `sqlalchemy.inspect()`, tables and views.
- `GET /connections/{id}/tables/{table_name}/columns` names plus types.

### Saved queries
- `POST /connections/{id}/queries` validate through the guard, then dry-run
  with a 1-row fetch cap against this connection before persisting. Validation
  or dry-run failure returns the specific error and persists nothing.
- `GET /connections/{id}/queries`, `GET|PUT|DELETE /queries/{id}`.

### Execution
- `POST /queries/{id}/run` returns query_id, executed_at, duration_ms,
  row_count, truncated, data_hash, columns, rows, chart. Always executes and
  refreshes the cache.
- `GET /queries/{id}/poll?since_hash=` returns `{"changed": false, "data_hash",
  "poll_interval_ms"}` on a hash match, otherwise the full run payload plus
  `"changed": true`.
- `POST /connections/{id}/query/preview` ad-hoc SQL through the same guard,
  never persisted, row cap 100.

### Poll cache
Per-query in-process entry `(data_hash, payload, fetched_at)` with TTL equal to
that query's `poll_interval_ms`. A warm cache whose hash matches `since_hash`
returns `changed: false` with zero target-DB traffic. `/run` bypasses and
refreshes.

`data_hash` is `sha256` over canonical JSON of `{columns, rows}` using a
deterministic encoder for Decimal, date, datetime, UUID, bytes, and None.

## 7. Error taxonomy

| HTTP | error_code |
|---|---|
| 400 | MULTIPLE_STATEMENTS, NON_SELECT_STATEMENT, FORBIDDEN_KEYWORD, FORBIDDEN_FUNCTION, EMPTY_STATEMENT, INVALID_SQL, QUERY_EXECUTION_ERROR, INVALID_CHART_CONFIG, ROW_LIMIT_EXCEEDED |
| 401 | DB_AUTH_FAILED |
| 403 | DB_PERMISSION_DENIED |
| 404 | CONNECTION_NOT_FOUND, QUERY_NOT_FOUND, TABLE_NOT_FOUND |
| 409 | DUPLICATE_NAME |
| 502 | DB_UNREACHABLE |
| 504 | QUERY_TIMEOUT |

Mapped off Postgres SQLSTATE (28P01, 28000, 42501, 57014, 08*) and MySQL errno
(1045, 1142, 1143, 3024, 2003, 2005), not off exception string matching. Every
error response is `{"error_code", "message", "detail"}`.

## 8. Non-functional

- Engine registry keyed by connection id, `pool_pre_ping=True`, bounded pool
  size, LRU eviction above a cap, disposed on connection update or delete,
  guarded by a lock.
- CORS origins configurable, default `*` for local dev.
- Config env vars, all with dev defaults: `FAE_APP_DB_URL`, `FAE_FERNET_KEY`,
  `FAE_DEFAULT_ROW_LIMIT`, `FAE_MAX_ROW_LIMIT`, `FAE_PREVIEW_ROW_LIMIT`,
  `FAE_QUERY_TIMEOUT_S`, `FAE_POLL_INTERVAL_MS`, `FAE_CORS_ORIGINS`,
  `FAE_TARGET_POOL_SIZE`, `FAE_MAX_TARGET_ENGINES`.
- Missing `FAE_FERNET_KEY` in dev generates a key into a gitignored
  `.secrets/fernet.key` with a loud warning. Never committed.
- Alembic for app-state schema.

## 9. Testing

- Unit: adversarial SQL corpus (statement stacking, comment tricks, CTE-wrapped
  INSERT, WITH plus DDL, function exfil, casing and whitespace variants),
  crypto round-trip, hash determinism, error mapping.
- Integration: full connections and saved-queries CRUD against a temp SQLite
  target, dry-run-on-save rejection, cascade delete, poll cache hit counted.

There is no LLM in this service, so there is no eval suite to write. The
adversarial guard corpus is the equivalent quality gate, stated as such in the
README rather than faking an eval harness.

## 10. Decisions taken

- Layout: `services/analyzer/` per CLAUDE.md, not the brief's bare `app/`.
- Poll: short-TTL result cache.
- Delete connection: cascade.
- Row cap: fetch-side, not SQL rewrite.
- MySQL driver: `pymysql`, pure Python, no build step.
- Single-tenant. No auth, no multi-user.
