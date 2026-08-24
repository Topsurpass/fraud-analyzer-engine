# Analyzer API contract

Frozen response shapes and the full `error_code` table. The generated
`openapi.json` in this directory is the machine-readable version; regenerate it
with `uv run python ../../scripts/export_openapi.py` from `services/analyzer/`.

## Error envelope

Every non-2xx response, without exception, has this shape:

```json
{ "error_code": "MULTIPLE_STATEMENTS", "message": "...", "detail": { } }
```

`detail` is `null` or an object. Branch on `error_code`, never on `message`.

| error_code | HTTP | Meaning |
|---|---|---|
| `EMPTY_STATEMENT` | 400 | No SQL, or nothing left after comments were stripped |
| `MULTIPLE_STATEMENTS` | 400 | More than one statement was submitted |
| `NON_SELECT_STATEMENT` | 400 | The statement is not a SELECT or a `WITH ... SELECT` |
| `FORBIDDEN_KEYWORD` | 400 | A write or exfil keyword appears in the statement |
| `FORBIDDEN_FUNCTION` | 400 | A filesystem, network, or sleep function was called |
| `INVALID_SQL` | 400 | The statement could not be parsed |
| `QUERY_EXECUTION_ERROR` | 400 | Valid read-only SQL that this database rejected |
| `INVALID_CHART_CONFIG` | 400 | The chart mapping is not usable |
| `INVALID_CONNECTION_CONFIG` | 400 | Fields supplied do not match the `db_type` |
| `ROW_LIMIT_EXCEEDED` | 400 | `row_limit` is below 1 or above the ceiling |
| `SQL_TOO_LONG` | 400 | The statement is longer than `FAE_MAX_SQL_LENGTH` |
| `RESULT_TOO_LARGE` | 400 | The result passed `FAE_MAX_RESULT_BYTES` while being read |
| `DB_AUTH_FAILED` | 401 | The target database rejected the credentials |
| `DB_PERMISSION_DENIED` | 403 | The role lacks rights, or a write hit a read-only session |
| `CONNECTION_NOT_FOUND` | 404 | No connection with that id |
| `QUERY_NOT_FOUND` | 404 | No saved query with that id |
| `TABLE_NOT_FOUND` | 404 | No such table or view on that connection |
| `DASHBOARD_NOT_FOUND` | 404 | No dashboard with that id |
| `DUPLICATE_NAME` | 409 | A connection, query, or dashboard already has that name |
| `REQUEST_VALIDATION_ERROR` | 422 | The request body or query params failed validation |
| `RATE_LIMITED` | 429 | Per-client request budget exhausted; see `Retry-After` |
| `INTERNAL_ERROR` | 500 | Unexpected failure; details are logged, never returned |
| `DB_UNREACHABLE` | 502 | Could not open a connection to the target |
| `QUERY_TIMEOUT` | 504 | The statement exceeded the timeout |
| `SERVICE_NOT_READY` | 503 | `/ready` only: the app-state database is unreachable |

Two notes on codes that changed behaviour:

* `INVALID_SQL` was previously unreachable. Every path to it was marked
  defensive, and a real `sqlparse` failure escaped as an uncaught 500. It is
  now emitted whenever the parser refuses the input, including at sqlparse's
  own 10,000-token ceiling.
* `QUERY_TIMEOUT` used to be returned for pool exhaustion, where the query had
  never run. That case is now `DB_UNREACHABLE` with
  `detail.reason = "pool_exhausted"`, and the pool configuration is no longer
  included in the message.

## `POST /queries/{id}/run`

```json
{
  "query_id": "9f1c...",
  "executed_at": "2026-08-22T12:00:00Z",
  "duration_ms": 42,
  "row_count": 137,
  "truncated": false,
  "data_hash": "sha256:...",
  "columns": ["day", "flagged_count"],
  "rows": [["2026-08-20", 12], ["2026-08-21", 9]],
  "chart": {
    "type": "line",
    "x_field": "day",
    "y_field": "flagged_count",
    "series_field": null,
    "warnings": []
  },
  "flags": {
    "flagged_count": 1,
    "rows": [{"index": 0, "rule_ids": ["a1b2..."]}],
    "rules": [
      {"id": "a1b2...", "name": "Large transfer", "severity": "high", "matched": 1}
    ],
    "warnings": []
  },
  "poll_interval_ms": 5000
}
```

`truncated` is `true` when the result set was larger than the query's
`row_limit`. `chart.warnings` is non-empty when the chart mapping names a
column the result does not contain; the rows are still returned.

`flags` is always present and is empty when the query defines no flag rules, so
a client never has to branch on the key existing. `flags.rows` carries **only
flagged rows**; `index` is a position in `rows`. `data_hash` covers the flag
outcome as well as the data, so editing a rule changes the hash even when not a
single row moved -- without that, polling would answer `changed: false` and the
edit would never reach the screen.

## `GET /queries/{id}/poll?since_hash=&force=`

Unchanged (the cheap path, no target-database traffic):

```json
{
  "query_id": "9f1c...",
  "changed": false,
  "data_hash": "sha256:...",
  "poll_interval_ms": 5000,
  "from_cache": true
}
```

Changed: the full `/run` payload plus `"changed": true` and `"from_cache"`.

Poll the endpoint every `poll_interval_ms`, passing the last `data_hash` you
received as `since_hash`. Pass `force=true` to bypass the cache.

## `POST /connections/{id}/query/preview`

```json
{
  "connection_id": "...",
  "executed_at": "2026-08-22T12:00:00Z",
  "duration_ms": 8,
  "row_count": 100,
  "truncated": true,
  "columns": ["..."],
  "rows": [["..."]],
  "flags": {"flagged_count": 0, "rows": [], "rules": [], "warnings": []}
}
```

Nothing is persisted and nothing is logged. Capped at `FAE_PREVIEW_ROW_LIMIT`
(default 100) regardless of what the request asks for.

The request may carry a `flag_rules` array in the same shape as
`PUT /queries/{id}/flag-rules` below. Those rules are evaluated against the
preview rows and thrown away, which is what lets an editor answer "would this
rule catch anything?" before the query is saved. Unsaved rules have no id, so
each is reported under its index in the submitted array.

## `GET|PUT /queries/{id}/flag-rules`

A flag rule is a named set of conditions that marks rows in that query's result.

```json
{
  "rules": [
    {
      "name": "Large transfer",
      "severity": "high",
      "enabled": true,
      "conditions": [
        {"column_name": "amount", "operator": "gt", "value": "500000"},
        {"column_name": "country", "operator": "neq", "value": "NG"}
      ]
    }
  ]
}
```

A rule matches a row when **all** of its conditions match. A row is flagged when
**any** enabled rule matches. That covers AND and OR without an expression
language, which is deliberate: conditions are evaluated in Python over rows the
database already returned, so nothing a user writes is ever spliced into SQL and
the same evaluation runs on Postgres, MySQL and SQLite. The consequence is that
flagging only sees rows inside the query's `row_limit`.

`PUT` replaces the whole set; `position` is the index in the submitted array, so
reordering needs no separate call, and an empty array removes every rule. Rule
names must be distinct within a query.

Operators: `gt`, `gte`, `lt`, `lte`, `eq`, `neq`, `contains`, `not_contains`,
`starts_with`, `in`, `not_in`, `is_null`, `is_not_null`, `between`. `is_null` and
`is_not_null` take no value; `between` takes `value` and `value2`; everything
else takes `value`. `in`/`not_in` split `value` on commas.

Comparison is numeric when both sides parse as numbers and lexical otherwise,
which is what makes `amount > 500` work against a `Decimal` that arrives as
`"500.25"` and `day >= '2026-08-19'` work on an ISO date string. `contains` and
`starts_with` are case-insensitive; `eq` and `neq` are not, matching `=` in
Postgres. A NULL cell matches `is_null` and nothing else, following SQL's
three-valued logic rather than Python's.

A condition naming a column the result does not contain matches nothing and is
reported in `flags.warnings`; the rows are still returned.

## `GET /connections/{id}/flagged`

Flagged rows across every rule-bearing query on one connection.

```json
{
  "connection_id": "...",
  "queries": [
    {
      "query_id": "...",
      "query_name": "Large transfers",
      "columns": ["day", "amount"],
      "rows": [{"index": 1, "rule_ids": ["..."], "values": ["2026-08-19", 900.0]}],
      "rules": [{"id": "...", "name": "Large", "severity": "high", "matched": 1}],
      "warnings": [],
      "flagged_count": 1,
      "executed_at": "2026-08-24T12:00:00Z",
      "stale": false,
      "error_code": null,
      "error_message": null
    }
  ],
  "flagged_count": 1,
  "refreshed": false,
  "refresh_truncated": false
}
```

Reads the poll cache and **runs nothing**, so opening this view costs the target
database nothing. A query with no cached result comes back `stale: true` rather
than being omitted, so the UI can say "not run yet" instead of implying the
rules matched nothing. Queries with no rules are omitted entirely.

Only flagged rows are carried, not the whole result: returning every row so the
client could filter would multiply the payload by the inverse of the flag rate.

## `POST /connections/{id}/flagged/refresh`

Same response shape, but re-runs each query first. The only path here that
touches the target database, so it counts against the **execution** rate-limit
bucket and is bounded by `FAE_FLAGGED_REFRESH_MAX_QUERIES` (default 20);
`refresh_truncated` is `true` when that bound applied. A query that fails comes
back with `error_code` set and `stale: true` while every other query on the
connection still reports its flagged rows.

## `POST /queries/poll`

Poll many saved queries in one request. Every chart on a dashboard otherwise
runs its own loop: twelve cards at the five-second default is twelve requests
per tick, each taking a worker thread, an app-state session, and on a cache
miss a target connection.

```json
{ "queries": [ { "query_id": "...", "since_hash": "sha256:..." } ], "force": false }
```

`queries` holds 1 to 100 items. The response is one result per query, in the
order submitted:

```json
{ "results": [ { "query_id": "...", "changed": true, "...": "..." } ] }
```

Each entry is the same shape `GET /queries/{id}/poll` returns. A query that
fails yields an error entry instead, so one broken card cannot blank out the
others on the board:

```json
{ "query_id": "...", "ok": false, "error_code": "QUERY_EXECUTION_ERROR",
  "message": "...", "detail": null }
```

The batch endpoint shares its implementation with the single one, so the two
cannot drift on caching, hashing, or logging.

## `GET /queries?ids=a,b,c`

Resolve many saved queries in one request. A dashboard card resolves to a saved
query and a board may span connections, so the per-connection listing cannot
serve one. Returns them in the order asked for; unknown ids are omitted rather
than raising, so a board that just lost a query still renders the rest.

## Health and readiness

| Method | Path | Meaning |
|---|---|---|
| `GET` | `/health` | Liveness. Answers whenever the process is up, and deliberately touches no database: a liveness probe that fails on a dependency outage turns that outage into a restart loop. |
| `GET` | `/ready` | Readiness. Runs `SELECT 1` against the app-state database. 200 `{"status":"ready"}`, or 503 `SERVICE_NOT_READY`. |

Point an orchestrator's liveness check at `/health` and its readiness check at
`/ready`. Allow a generous readiness grace period: a suspended serverless
Postgres can take over ten seconds just to accept a connection, and startup
runs migrations before serving.

## Rate limiting

Two per-client-IP budgets, since the service has no authentication:
`FAE_RATE_LIMIT_PER_MINUTE` (default 600) for general traffic and
`FAE_RATE_LIMIT_EXECUTION_PER_MINUTE` (default 300) for anything that opens a
connection to a target database. Exceeding one returns 429 `RATE_LIMITED` with
a `Retry-After` header. Set either to 0 to disable it.

Budgets are per process, so behind several instances the effective limit
multiplies by instance count.

## Request correlation

Every response carries `X-Request-ID`. An inbound `X-Request-ID` is honoured so
a trace started at a proxy or in the frontend carries through; otherwise one is
generated. The same id is attached to every log line emitted while serving that
request.

## Dashboards

A dashboard is a named, ordered arrangement of saved queries. It holds no SQL of
its own and never touches a target database; every card on it resolves through
the saved-query endpoints. A dashboard may span connections, which is the point
of having one.

| Method | Path |
|---|---|
| `GET` | `/dashboards` |
| `POST` | `/dashboards` |
| `GET` | `/dashboards/{dashboard_id}` |
| `PUT` | `/dashboards/{dashboard_id}` |
| `DELETE` | `/dashboards/{dashboard_id}` |

```json
{
  "id": "0f0c...",
  "name": "Card testing",
  "query_ids": ["8a1f...", "4634..."],
  "created_at": "2026-08-23T09:12:44Z",
  "updated_at": "2026-08-23T09:31:02Z"
}
```

`query_ids` is the display order. On `POST` and `PUT` it is taken literally:
duplicates collapse to their first position, and every id must name an existing
saved query or the whole write is refused with `QUERY_NOT_FOUND` and nothing is
persisted. A board pointing at a query that does not exist would render a card
that can only ever error, so the reference is checked on write rather than
discovered on read.

`PUT` is a partial update on `name`, but `query_ids` **replaces** the whole
arrangement rather than merging into it: a dashboard is an ordered list, and a
partial merge has no well-defined meaning for order. Omit `query_ids` to rename
without touching the cards; send `[]` to empty the board.

Membership is a table, not a JSON column of ids, so the database keeps the
reference honest. Deleting a saved query removes it from every dashboard that
showed it, and deleting a connection cascades through its queries to the same
effect. Deleting a dashboard is the reverse: the board goes, the saved queries
it showed are untouched.

## Credentials

No response model in this API declares a `password` or `password_encrypted`
field. Credentials cannot appear in any response body, including
`GET /connections`.
