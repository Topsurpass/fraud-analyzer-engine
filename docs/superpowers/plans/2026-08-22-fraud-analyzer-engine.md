# Fraud Analyzer Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A schema-agnostic FastAPI backend that stores DB connection profiles and named SELECT-only queries, then serves each query's results as chart-ready JSON with a cheap poll endpoint.

**Architecture:** One service under `services/analyzer/`. A validator (`sql_guard`) gates every statement before it reaches a target DB. A per-connection SQLAlchemy engine registry supplies read-only, timeout-bounded connections. A query service executes, caps rows fetch-side, hashes the payload, and shapes the chart block. A TTL cache makes polling free when nothing changed.

**Tech Stack:** Python 3.12 (uv), FastAPI, Pydantic v2, SQLAlchemy 2.0 Core, Alembic, sqlparse, cryptography (Fernet), psycopg[binary], PyMySQL, uvicorn, pytest.

**Spec:** `docs/superpowers/specs/2026-08-22-fraud-analyzer-engine-design.md`

## Global Constraints

- Python 3.12 pinned via `uv`. System Python 3.8.10 is too old for Pydantic v2 + psycopg3.
- All paths relative to `services/analyzer/` unless stated. Run commands from that directory.
- No credential field (`password`, `password_encrypted`) may appear in any response model. Response models must not declare them.
- Plaintext passwords are never persisted. Fernet only.
- Every guard rejection returns HTTP 400 with a distinct `error_code` from `app/errors.py`.
- Row caps are enforced fetch-side (`fetchmany(n + 1)`). Never rewrite user SQL to add LIMIT.
- Hard ceiling on `row_limit` is 10000. Default 1000. Preview cap 100.
- Default statement timeout 10s, configurable via `FAE_QUERY_TIMEOUT_S`.
- No LLM in this service, therefore no eval suite. The adversarial guard corpus is the quality gate.
- Env var prefix is `FAE_`.

---

### Task 1: Scaffold, config, error taxonomy

**Files:**
- Create: `pyproject.toml`, `.python-version`, `app/__init__.py`, `app/config.py`, `app/errors.py`, `.gitignore` (repo root), `tests/__init__.py`, `tests/conftest.py`
- Test: `tests/test_config.py`, `tests/test_errors.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `app.config.Settings` (pydantic-settings `BaseSettings`, `env_prefix="FAE_"`) with fields `app_db_url: str`, `fernet_key: str | None`, `default_row_limit: int = 1000`, `max_row_limit: int = 10000`, `preview_row_limit: int = 100`, `query_timeout_s: int = 10`, `poll_interval_ms: int = 5000`, `cors_origins: str = "*"`, `target_pool_size: int = 5`, `max_target_engines: int = 32`
  - `app.config.get_settings() -> Settings` (lru_cache'd)
  - `app.errors.ErrorCode` (str Enum) with every member from spec section 7
  - `app.errors.AppError(Exception)` with `.error_code: ErrorCode`, `.message: str`, `.detail: dict | None`, `.http_status: int`
  - Subclasses: `SqlValidationError`, `DbAuthError`, `DbPermissionError`, `DbUnreachableError`, `QueryTimeoutError`, `NotFoundError`, `DuplicateNameError`, `QueryExecutionError`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_errors.py
from app.errors import AppError, ErrorCode, SqlValidationError, QueryTimeoutError

def test_sql_validation_error_is_400():
    e = SqlValidationError(ErrorCode.MULTIPLE_STATEMENTS, "two statements")
    assert e.http_status == 400
    assert e.error_code == ErrorCode.MULTIPLE_STATEMENTS

def test_timeout_error_is_504():
    assert QueryTimeoutError("slow").http_status == 504

def test_to_response_has_no_extra_keys():
    body = SqlValidationError(ErrorCode.INVALID_SQL, "bad").to_response()
    assert set(body) == {"error_code", "message", "detail"}
```

```python
# tests/test_config.py
from app.config import Settings

def test_defaults():
    s = Settings(_env_file=None)
    assert s.default_row_limit == 1000
    assert s.max_row_limit == 10000
    assert s.query_timeout_s == 10

def test_env_prefix(monkeypatch):
    monkeypatch.setenv("FAE_DEFAULT_ROW_LIMIT", "50")
    assert Settings(_env_file=None).default_row_limit == 50
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_errors.py tests/test_config.py -v`. Expected: collection error, module not found.
- [ ] **Step 3: Write `pyproject.toml`, `.python-version` (3.12), `.gitignore` (`.secrets/`, `*.db`, `__pycache__/`, `.venv/`, `.env`), `app/config.py`, `app/errors.py`.**
- [ ] **Step 4: Run tests** — expected PASS.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(analyzer): scaffold, settings, error taxonomy"`

---

### Task 2: Credential encryption

**Files:**
- Create: `app/security/__init__.py`, `app/security/crypto.py`
- Test: `tests/test_crypto.py`

**Interfaces:**
- Consumes: `app.config.get_settings`
- Produces: `encrypt(plaintext: str) -> str`, `decrypt(token: str) -> str`, `get_fernet() -> Fernet`, `generate_key() -> str`

Key resolution order: `FAE_FERNET_KEY` env var, then `.secrets/fernet.key` on disk, then generate one, write it to `.secrets/fernet.key` with mode 0600, and log a warning naming the file.

- [ ] **Step 1: Write failing tests**

```python
def test_round_trip():
    assert decrypt(encrypt("hunter2")) == "hunter2"

def test_ciphertext_is_not_plaintext():
    assert "hunter2" not in encrypt("hunter2")

def test_two_encryptions_differ():
    assert encrypt("x") != encrypt("x")   # Fernet IV is random

def test_missing_key_generates_and_persists(tmp_path, monkeypatch):
    monkeypatch.delenv("FAE_FERNET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    get_fernet.cache_clear()
    encrypt("a")
    assert (tmp_path / ".secrets" / "fernet.key").exists()

def test_tampered_token_raises():
    import pytest
    from cryptography.fernet import InvalidToken
    with pytest.raises(InvalidToken):
        decrypt(encrypt("x")[:-4] + "AAAA")
```

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement `app/security/crypto.py`.**
- [ ] **Step 4: Run tests, expected PASS.**
- [ ] **Step 5: Commit** — `"feat(analyzer): Fernet credential encryption"`

---

### Task 3: SQL guard (highest risk, gates everything after it)

**Files:**
- Create: `app/security/sql_guard.py`
- Test: `tests/test_sql_guard.py`

**Interfaces:**
- Consumes: `app.errors.SqlValidationError`, `app.errors.ErrorCode`
- Produces:
  - `validate_select(sql: str) -> str` returns the comment-stripped, trimmed SQL that is safe to execute; raises `SqlValidationError` otherwise.
  - `FORBIDDEN_KEYWORDS: frozenset[str]`, `FORBIDDEN_FUNCTIONS: frozenset[str]`

Algorithm, in this order (order is load-bearing):
1. Reject empty/whitespace-only input with `EMPTY_STATEMENT`.
2. `stripped = sqlparse.format(sql, strip_comments=True).strip()`. Re-check empty.
3. `parts = [p for p in sqlparse.split(stripped) if p.strip().rstrip(";").strip()]`. `len(parts) > 1` raises `MULTIPLE_STATEMENTS`.
4. Parse the single part. `stmt.get_type()` must be `SELECT`; otherwise if the first meaningful token is the CTE keyword `WITH`, accept provisionally and let step 5 catch `WITH ... INSERT`. Anything else raises `NON_SELECT_STATEMENT`.
5. Walk `stmt.flatten()`. For tokens whose `ttype` is in `(Keyword, Keyword.DML, Keyword.DDL, Keyword.CTE)`, uppercase the value and reject on membership in `FORBIDDEN_KEYWORDS` with `FORBIDDEN_KEYWORD`, naming the token.
6. Walk `stmt.flatten()` for `Name` tokens immediately followed (skipping whitespace) by a `Punctuation` `(`. Lowercase and reject on membership in `FORBIDDEN_FUNCTIONS` with `FORBIDDEN_FUNCTION`.
7. Return `stripped` with a single trailing `;` removed.

`FORBIDDEN_KEYWORDS` = INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE, GRANT, REVOKE, ATTACH, DETACH, PRAGMA, EXEC, EXECUTE, CALL, MERGE, REPLACE, COPY, LOAD, VACUUM, REINDEX, SET, LOCK, UNLOCK, USE, HANDLER, DO, INTO, RENAME, COMMENT, ANALYZE, BEGIN, COMMIT, ROLLBACK, SAVEPOINT, PREPARE, DEALLOCATE, DECLARE, FETCH, CLOSE, LISTEN, NOTIFY, UNLISTEN, DISCARD, CLUSTER, REFRESH, IMPORT, OUTFILE, DUMPFILE.

`FORBIDDEN_FUNCTIONS` = pg_read_file, pg_read_binary_file, pg_ls_dir, pg_stat_file, pg_sleep, lo_import, lo_export, dblink, dblink_exec, query_to_xml, load_file, readfile, writefile, load_extension, sleep, benchmark.

- [ ] **Step 1: Write the adversarial corpus as failing tests**

```python
import pytest
from app.errors import ErrorCode, SqlValidationError
from app.security.sql_guard import validate_select

ACCEPTED = [
    "SELECT 1",
    "select * from txns",
    "SELECT day, count(*) FROM txns GROUP BY day ORDER BY day",
    "WITH f AS (SELECT * FROM txns WHERE flagged) SELECT count(*) FROM f",
    "SELECT a FROM t UNION ALL SELECT b FROM u",
    "SELECT 'DROP TABLE users' AS note",          # keyword inside a literal
    "SELECT created_at, updated_at FROM t",       # keyword as substring of a name
    "SELECT * FROM t LIMIT 10;",                  # single trailing semicolon
    "SELECT sum(amount) OVER (PARTITION BY user_id) FROM txns",
]

REJECTED = [
    ("SELECT 1; DROP TABLE users",            ErrorCode.MULTIPLE_STATEMENTS),
    ("SELECT 1 -- \n; DROP TABLE users",      ErrorCode.MULTIPLE_STATEMENTS),
    ("SELECT 1 /* x */; DELETE FROM t",       ErrorCode.MULTIPLE_STATEMENTS),
    ("DROP TABLE users",                      ErrorCode.NON_SELECT_STATEMENT),
    ("DELETE FROM txns",                      ErrorCode.NON_SELECT_STATEMENT),
    ("UPDATE t SET a = 1",                    ErrorCode.NON_SELECT_STATEMENT),
    ("INSERT INTO t VALUES (1)",              ErrorCode.NON_SELECT_STATEMENT),
    ("TRUNCATE TABLE t",                      ErrorCode.NON_SELECT_STATEMENT),
    ("ATTACH DATABASE 'x.db' AS x",           ErrorCode.NON_SELECT_STATEMENT),
    ("PRAGMA table_info(t)",                  ErrorCode.NON_SELECT_STATEMENT),
    ("WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x",
                                              ErrorCode.FORBIDDEN_KEYWORD),
    ("WITH x AS (SELECT 1) DELETE FROM t",    ErrorCode.FORBIDDEN_KEYWORD),
    ("SELECT * INTO newtbl FROM t",           ErrorCode.FORBIDDEN_KEYWORD),
    ("SELECT * FROM t INTO OUTFILE '/tmp/x'", ErrorCode.FORBIDDEN_KEYWORD),
    ("SELECT pg_read_file('/etc/passwd')",    ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT load_file('/etc/passwd')",       ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT readfile('/etc/passwd')",        ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT load_extension('evil.so')",      ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT PG_READ_FILE('/etc/passwd')",    ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT dblink('h','SELECT 1')",         ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT pg_sleep(30)",                   ErrorCode.FORBIDDEN_FUNCTION),
    ("",                                      ErrorCode.EMPTY_STATEMENT),
    ("   \n\t ",                              ErrorCode.EMPTY_STATEMENT),
    ("-- just a comment",                     ErrorCode.EMPTY_STATEMENT),
    ("GRANT ALL ON t TO public",              ErrorCode.NON_SELECT_STATEMENT),
    ("COPY t TO PROGRAM 'sh -c whoami'",      ErrorCode.NON_SELECT_STATEMENT),
]

@pytest.mark.parametrize("sql", ACCEPTED)
def test_accepted(sql):
    assert validate_select(sql)

@pytest.mark.parametrize("sql,code", REJECTED)
def test_rejected(sql, code):
    with pytest.raises(SqlValidationError) as ei:
        validate_select(sql)
    assert ei.value.error_code == code

def test_comments_are_stripped_from_returned_sql():
    out = validate_select("SELECT 1 /* hi */ FROM t -- tail")
    assert "hi" not in out and "tail" not in out

def test_trailing_semicolon_removed():
    assert not validate_select("SELECT 1;").endswith(";")
```

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement `app/security/sql_guard.py`.**
- [ ] **Step 4: Run tests. Every case must pass. A single bypass blocks the rest of the plan.**
- [ ] **Step 5: Commit** — `"feat(analyzer): SELECT-only SQL guard with adversarial corpus"`

---

### Task 4: App-state models and Alembic migration

**Files:**
- Create: `app/models/__init__.py`, `app/models/base.py`, `app/models/connection.py`, `app/models/saved_query.py`, `app/models/execution_log.py`, `app/db/__init__.py`, `app/db/app_state.py`, `alembic.ini`, `alembic/env.py`, `alembic/versions/0001_initial.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: `app.config.get_settings`
- Produces:
  - `app.models.base.Base` (DeclarativeBase), `TimestampMixin` (`created_at`, `updated_at`)
  - `Connection`, `SavedQuery`, `QueryExecutionLog` with columns per spec section 4
  - `DbType`, `ConnectionStatus`, `ChartType` str enums
  - `app.db.app_state.get_session() -> Generator[Session]`, `init_db()`, `SessionLocal`

`SavedQuery.connection_id` is `ForeignKey("connections.id", ondelete="CASCADE")`; the relationship uses `cascade="all, delete-orphan"`. SQLite needs `PRAGMA foreign_keys=ON` set in a `connect` event listener on the app-state engine.

- [ ] **Step 1: Write failing tests** covering: creating a Connection defaults `status` to `untested`; deleting a Connection deletes its SavedQuery rows and their QueryExecutionLog rows; `row_limit` defaults to 1000; unique constraint on `(connection_id, name)` raises `IntegrityError`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement models, `app_state.py`, and the Alembic scaffold. Generate `0001_initial` and check it in.**
- [ ] **Step 4: Run tests plus `uv run alembic upgrade head` against a temp SQLite file. Both must succeed.**
- [ ] **Step 5: Commit** — `"feat(analyzer): app-state models and initial migration"`

---

### Task 5: Target engine registry

**Files:**
- Create: `app/db/target_registry.py`
- Test: `tests/test_target_registry.py`

**Interfaces:**
- Consumes: `Connection` model, `app.security.crypto.decrypt`, `app.config.get_settings`, error classes
- Produces:
  - `build_url(conn: Connection) -> URL`
  - `get_engine(conn: Connection) -> Engine` (cached by `conn.id`, LRU-evicted at `max_target_engines`, thread-safe)
  - `dispose_engine(connection_id: str) -> None`
  - `dispose_all() -> None`
  - `read_only_connection(conn: Connection) -> ContextManager[Connection]` yields a DBAPI-level SQLAlchemy connection already inside a read-only transaction with the timeout applied
  - `translate_db_error(exc: Exception) -> AppError`

Per-driver settings (spec section 5):
- postgres: `psycopg`, `connect_args={"options": f"-c default_transaction_read_only=on -c statement_timeout={ms}", "connect_timeout": ...}`
- mysql: `pymysql`, `connect_args={"connect_timeout":…, "read_timeout":…}`, plus a `connect` event issuing `SET SESSION TRANSACTION READ ONLY` and `SET SESSION max_execution_time = <ms>`
- sqlite: `creator=lambda: sqlite3.connect(f"file:{path}?mode=ro", uri=True)`, plus a progress handler that raises past the deadline

`translate_db_error` maps Postgres SQLSTATE `28P01`/`28000` and MySQL errno 1045 to `DbAuthError` (401); SQLSTATE `42501` and MySQL 1142/1143 to `DbPermissionError` (403); SQLSTATE `57014` and MySQL 3024 to `QueryTimeoutError` (504); SQLSTATE class `08` and MySQL 2003/2005 to `DbUnreachableError` (502); everything else to `QueryExecutionError` (400).

- [ ] **Step 1: Write failing tests** — SQLite engine is read-only (an `INSERT` through it raises), `get_engine` returns the same object twice for one connection id, `dispose_engine` forces a new object, LRU eviction disposes the oldest past the cap, `translate_db_error` maps a fabricated SQLSTATE 28P01 error to a 401 `DbAuthError`, and a SQLite query exceeding the deadline raises `QueryTimeoutError`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement `app/db/target_registry.py`.**
- [ ] **Step 4: Run tests, expected PASS.**
- [ ] **Step 5: Commit** — `"feat(analyzer): pooled read-only target engine registry"`

---

### Task 6: Connection service, connections router, introspection router

**Files:**
- Create: `app/schemas/__init__.py`, `app/schemas/connection.py`, `app/services/__init__.py`, `app/services/connection_service.py`, `app/routers/__init__.py`, `app/routers/connections.py`, `app/routers/introspection.py`, `app/main.py`
- Test: `tests/test_connections_api.py`, `tests/test_introspection_api.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 4, 5
- Produces:
  - `ConnectionCreate`, `ConnectionUpdate`, `ConnectionRead` (no credential fields declared), `ConnectionTestResult`, `TableList`, `ColumnInfo`
  - `connection_service.create_connection`, `list_connections`, `get_connection`, `test_connection`, `delete_connection`
  - `app.main.app` (FastAPI), CORS middleware, `AppError` exception handler returning `{"error_code","message","detail"}`

`ConnectionCreate` validates per `db_type`: sqlite requires `sqlite_path` and forbids host/port/username/password; postgres and mysql require host/database/username. `POST /connections` tests immediately, persists either way, and returns 201 with `status` set and `test_error` populated on failure.

- [ ] **Step 1: Write failing API tests** with `TestClient` against a temp SQLite target file: create returns 201 with `status == "ok"`; the response body contains no `password` or `password_encrypted` key; create against a nonexistent sqlite path returns 201 with `status == "failed"` and a non-null `test_error`; `GET /connections` lists it without credentials; `POST /{id}/test` flips status; `DELETE` returns 204 and cascades; `GET /connections/{missing}` returns 404 with `error_code == "CONNECTION_NOT_FOUND"`; `GET /{id}/tables` lists the seeded table; `GET /{id}/tables/{name}/columns` returns the seeded column names and types; unknown table returns 404 `TABLE_NOT_FOUND`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement schemas, service, both routers, and `main.py`.**
- [ ] **Step 4: Run tests, expected PASS.**
- [ ] **Step 5: Commit** — `"feat(analyzer): connections CRUD and schema introspection"`

---

### Task 7: Query execution engine, hashing, chart shaping, result cache

**Files:**
- Create: `app/services/query_service.py`, `app/services/result_cache.py`, `app/schemas/query.py`
- Test: `tests/test_query_service.py`, `tests/test_result_cache.py`

**Interfaces:**
- Consumes: Tasks 3, 5
- Produces:
  - `execute_sql(conn: Connection, sql: str, row_limit: int) -> ExecutionResult` — a dataclass carrying `columns: list[str]`, `rows: list[list]`, `row_count: int`, `truncated: bool`, `duration_ms: int`
  - `canonical_hash(columns, rows) -> str` returning `"sha256:<hex>"`, deterministic across Decimal, date, datetime, UUID, bytes, None
  - `build_chart(q: SavedQuery, columns: list[str]) -> dict` with keys `type`, `x_field`, `y_field`, `series_field`, `warnings`
  - `run_saved_query(session, q, use_cache: bool) -> RunPayload`
  - `result_cache.get(query_id) -> CacheEntry | None`, `set(query_id, entry, ttl_ms)`, `invalidate(query_id)`, `clear()`

`execute_sql` calls `validate_select` first, opens `read_only_connection`, executes, and calls `fetchmany(row_limit + 1)`. Receiving `row_limit + 1` rows sets `truncated=True` and trims to `row_limit`. Values are JSON-coerced (Decimal to float, date/datetime to ISO 8601, UUID to str, bytes to base64) before hashing and serialization, so hash input and response body are the same bytes.

`build_chart` emits a warning string when `x_field`, `y_field`, or `series_field` names a column absent from `columns`, without failing the run.

- [ ] **Step 1: Write failing tests** — a 5-row table with `row_limit=3` yields 3 rows and `truncated is True`; `row_limit=10` yields `truncated is False`; the same result hashes identically across two runs and differs after the underlying row changes; a Decimal/date/UUID/bytes/None row round-trips through `json.dumps` without error; a `DROP TABLE` passed to `execute_sql` raises `SqlValidationError` before touching the DB; `build_chart` with `y_field="missing"` returns a warning and still returns the chart block; cache entries expire after their TTL and `invalidate` drops them.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement `query_service.py`, `result_cache.py`, `schemas/query.py`.**
- [ ] **Step 4: Run tests, expected PASS.**
- [ ] **Step 5: Commit** — `"feat(analyzer): query execution, stable hashing, chart shaping, TTL cache"`

---

### Task 8: Saved-query CRUD, run, poll, preview

**Files:**
- Create: `app/routers/queries.py`
- Modify: `app/main.py` (include the router)
- Test: `tests/test_queries_api.py`, `tests/test_run_poll_api.py`

**Interfaces:**
- Consumes: Tasks 6, 7
- Produces endpoints: `POST /connections/{id}/queries`, `GET /connections/{id}/queries`, `GET|PUT|DELETE /queries/{id}`, `POST /queries/{id}/run`, `GET /queries/{id}/poll`, `POST /connections/{id}/query/preview`

Save path: `validate_select`, then a dry run with `row_limit=1` against this connection. Failure returns the specific error and persists nothing. Success persists and returns 201. `PUT` re-validates and re-dry-runs, then invalidates the cache entry. `row_limit` above `max_row_limit` returns 400 `ROW_LIMIT_EXCEEDED`.

`/run` always executes, refreshes the cache, and writes a `QueryExecutionLog` row (success or failure). `/poll` consults the cache first; on a warm entry whose hash equals `since_hash` it returns `{"changed": false, "data_hash", "poll_interval_ms"}` with no target-DB traffic. Otherwise it executes and returns the full payload plus `"changed": true`.

- [ ] **Step 1: Write failing API tests** — saving `DROP TABLE t` returns 400 `NON_SELECT_STATEMENT` and creates no row; saving `SELECT * FROM no_such_table` returns 400 `QUERY_EXECUTION_ERROR` and creates no row; a valid save returns 201; `/run` returns the exact key set from the spec's example payload; a second `/run` returns the same `data_hash`; `/poll?since_hash=<that hash>` returns `changed: false`; `/poll` with a stale hash returns `changed: true` plus rows; after inserting a row into the target and clearing the cache, `data_hash` changes; `/preview` caps at 100 rows and persists nothing; `row_limit=999999` returns 400 `ROW_LIMIT_EXCEEDED`; a failed `/run` still writes an execution log row with `success=False`.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement `app/routers/queries.py` and wire it into `main.py`.**
- [ ] **Step 4: Run the whole suite, expected PASS.**
- [ ] **Step 5: Commit** — `"feat(analyzer): saved-query CRUD, run, poll, preview"`

---

### Task 9: Error-handling polish, contracts, docs, OpenAPI export

**Files:**
- Create: `services/analyzer/README.md`, `contracts/analyzer-api.md`, `scripts/export_openapi.py`, `README.md` (repo root), `.env.example`
- Modify: `app/main.py` (handlers for `AppError`, `RequestValidationError`, and a catch-all)
- Test: `tests/test_error_mapping.py`

**Interfaces:**
- Consumes: everything prior
- Produces: uniform `{"error_code","message","detail"}` on every non-2xx response; `scripts/export_openapi.py` writing `contracts/openapi.json`

README must carry, as its own heading, the read-only DB role recommendation: the app enforces read-only at the query layer, and a read-only DB user is the real backstop. It also documents cascade delete on connections, the fetch-side row cap, the env vars, `uv` setup, `alembic upgrade head`, and why there is no eval suite.

- [ ] **Step 1: Write failing tests** — a Pydantic body-validation failure returns 422 with an `error_code` key present; an unhandled exception returns 500 with `error_code == "INTERNAL_ERROR"` and leaks no traceback in the body; every `ErrorCode` member maps to exactly one HTTP status.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement handlers, write the docs, write the export script.**
- [ ] **Step 4: Run the full suite plus `uv run python scripts/export_openapi.py`.**
- [ ] **Step 5: Commit** — `"feat(analyzer): error handling, CORS, docs, OpenAPI contract"`

---

## Self-Review

**Spec coverage:** Section 3 layout -> Task 1. Section 4 data model -> Task 4. Section 5 guard, row cap, read-only drivers -> Tasks 3 and 5. Section 6 endpoints -> Tasks 6 and 8, poll cache -> Tasks 7 and 8, `data_hash` -> Task 7. Section 7 error taxonomy -> Tasks 1, 5, 9. Section 8 non-functional -> Tasks 1, 5, 9. Section 9 testing -> every task. No gaps.

**Placeholders:** none. Every code step names its file and its assertions.

**Type consistency:** `validate_select` returns `str` in Task 3 and is consumed as `str` in Tasks 7 and 8. `ExecutionResult` fields defined in Task 7 are the fields serialized in Task 8. `translate_db_error` defined in Task 5 is the only DB-error mapper used in Tasks 6, 7, 8.
