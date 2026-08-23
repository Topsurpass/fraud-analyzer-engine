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
| `DB_AUTH_FAILED` | 401 | The target database rejected the credentials |
| `DB_PERMISSION_DENIED` | 403 | The role lacks rights, or a write hit a read-only session |
| `CONNECTION_NOT_FOUND` | 404 | No connection with that id |
| `QUERY_NOT_FOUND` | 404 | No saved query with that id |
| `TABLE_NOT_FOUND` | 404 | No such table or view on that connection |
| `DASHBOARD_NOT_FOUND` | 404 | No dashboard with that id |
| `DUPLICATE_NAME` | 409 | A connection, query, or dashboard already has that name |
| `REQUEST_VALIDATION_ERROR` | 422 | The request body or query params failed validation |
| `INTERNAL_ERROR` | 500 | Unexpected failure; details are logged, never returned |
| `DB_UNREACHABLE` | 502 | Could not open a connection to the target |
| `QUERY_TIMEOUT` | 504 | The statement exceeded the timeout |

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
  "poll_interval_ms": 5000
}
```

`truncated` is `true` when the result set was larger than the query's
`row_limit`. `chart.warnings` is non-empty when the chart mapping names a
column the result does not contain; the rows are still returned.

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
  "rows": [["..."]]
}
```

Nothing is persisted and nothing is logged. Capped at `FAE_PREVIEW_ROW_LIMIT`
(default 100) regardless of what the request asks for.

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
