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
| `LIST_NOT_FOUND` | 404 | No list with that id, or a rule names a `list_id` that does not exist (`detail.list_ids`) |
| `DUPLICATE_NAME` | 409 | A connection, query, or dashboard already has that name |
| `QUERY_FROZEN` | 409 | The query has a published chart, or one waiting for approval; unpublish or withdraw to edit (administrators may always edit) |
| `DEFINITION_CHANGED` | 409 | Approve with a `definition_fingerprint` that is not the stored definition's: it changed since it was reviewed and must be read again |
| `PUBLISH_NOT_PENDING` | 409 | Approve or reject on a chart nobody is waiting on (never requested, withdrawn, or already decided) |
| `LIST_NAME_TAKEN` | 409 | Another list has that name, ignoring case |
| `LIST_IN_USE` | 409 | A rule still reads the list; `detail.rules` is `[{rule_name, query_id, query_name}]` for queries the caller can see, `detail.hidden_rule_count` counts the rest |
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
  "from_cache": true,
  "executed_at": "2026-08-22T12:00:00"
}
```

Changed: the full `/run` payload plus `"changed": true` and `"from_cache"`.

Every answer says when the result it describes was produced (`executed_at`), so
a client can time its next poll for the moment the cached result goes stale
(`executed_at + poll_interval_ms`) instead of an interval after it happened to
mount. A run that returns the same rows leaves `data_hash` alone but moves
`executed_at`, so an "unchanged" answer carries it too.

A query runs against the target database **at most once per `poll_interval_ms`**
however often it is polled: inside the interval the answer is served from the
cache, and a stale answer is returned immediately while one refresh runs behind
it. A refresh that fails is not retried until a further interval has passed.
Editing a chart (its type, fields, or publishing it) does not run the query: the
cached result's chart mapping is redrawn in place. Pass `force=true` to bypass
the cache and run the query now; that is the only way a poll costs an extra
execution. After an edit, fetch the whole payload (poll without `since_hash`) to
receive the new mapping: the hash covers the data, not how it is drawn.

Poll the endpoint every `poll_interval_ms`, passing the last `data_hash` you
received as `since_hash`.

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
`starts_with`, `in`, `not_in`, `in_list`, `not_in_list`, `is_null`, `is_not_null`,
`between`. `is_null` and `is_not_null` take no value; `between` takes `value` and
`value2`; `in_list` and `not_in_list` take `list_id` (see Lists below) and no
value; everything else takes `value`. `in`/`not_in` split `value` on commas.
`list_id` is `null` on every condition that does not use a list, and is cleared
if sent with any other operator. Reads add `list_name` (the list's current name,
or `null`) to each condition. A `list_id` that names no list is refused with
`LIST_NOT_FOUND` and nothing is saved. The preview endpoint accepts the same
conditions.

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
      "shared": false,
      "owner_name": "Ada Lovelace",
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

The sections are the caller's own queries, every query for an administrator, and
any query that has a **published** chart: sharing a chart shares its alerts.
`shared` is true when the caller does not own the query, and `owner_name` is the
owner's display name (never an email). Rows exclude what *the caller* has
dismissed, and `dismissed_count` counts the caller's own dismissals; see
"Publishing, approval and shared alerts".

## `POST /connections/{id}/flagged/refresh`

Same response shape, but re-runs each query first. The only path here that
touches the target database, so it counts against the **execution** rate-limit
bucket and is bounded by `FAE_FLAGGED_REFRESH_MAX_QUERIES` (default 20);
`refresh_truncated` is `true` when that bound applied. A query that fails comes
back with `error_code` set and `stale: true` while every other query on the
connection still reports its flagged rows. It only re-runs queries the caller may
run (their own, or any for an administrator); a shared section is returned from
what is stored, so a viewer's click never executes somebody else's query.

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

## Publishing, approval and shared alerts

A chart is `private`, `pending` or `published` (`publish_status` on every chart;
`is_public` is true only when `published`). An analyst cannot publish: **asking is
all they can do**, and an administrator decides.

| Call | Who | Effect |
| --- | --- | --- |
| `POST /queries/charts/{id}/publish` | author; any admin | Admin: `published` at once. Author: `pending`. Already pending or published: no change. |
| `POST /queries/charts/{id}/publish/cancel` | author; any admin | `pending` becomes `private`; also clears a rejection notice. Does not unpublish. |
| `POST /queries/charts/{id}/unpublish` | publisher; any admin | Unchanged. An approval keeps the **author** as `published_by`, so the author may retract; an admin's own publish is the admin's to retract. |
| `GET /queries/charts/publish-requests` | admin | Every `pending` chart, oldest first: `[{chart, query_id, query_name, connection_id, connection_name, requested_by: {id, full_name, email}, requested_at, definition_fingerprint}]`. Others get `403 FORBIDDEN`. |
| `POST /queries/charts/{id}/publish/approve` | admin | Body `{"definition_fingerprint": string}` (required; `422` without it). `pending` becomes `published`, provided the fingerprint is the stored definition's (see below). |
| `POST /queries/charts/{id}/publish/reject` | admin | Body `{"reason": string \| null}` (at most 500 characters, body optional). `pending` becomes `private` with `publish_rejection`. |

Approve and reject on anything not `pending` are `409 PUBLISH_NOT_PENDING`; for a
non-admin they are `403 FORBIDDEN`. Every transition is atomic: it is a conditional
update on the state it was decided from, so an approval cannot publish a request that
was withdrawn or replaced while the administrator was deciding, and a withdrawal
cannot clear a publication that landed first (the loser changes nothing). A chart read carries
`publish_requested_at` (while pending), `publish_rejection`
(`{reason, rejected_at, rejected_by_name}`, until the author asks again or
withdraws) and `published_by_name`.

While a chart is `pending` its query is frozen for non-admins exactly as when it is
published (`409 QUERY_FROZEN`; the message says the request is waiting and that
withdrawing unfreezes it). That covers the query, its charts **and its flag
rules**: rules decide what viewers are alerted to and what an approver reviewed, so
they are as fixed as the SQL. An administrator may still edit, and doing so does
not change a pending chart's status. Every transition is audited
(`chart_published`, `chart_unpublished`, `chart_publish_requested`,
`chart_publish_approved`, `chart_publish_rejected`, `chart_publish_cancelled`).

### Approval is bound to the definition that was reviewed

`definition_fingerprint` is a sha256 hex over a canonical JSON of what an
administrator reviews: the query's `sql_text`, `row_limit` and `poll_interval_ms` (as
stored), the chart's `chart_type`, `x_field`, `y_field`, `series_field` and
`surge_threshold_pct`, and every rule in position order (name, severity, enabled, and
each condition's column, operator, `value`, `value2` and `list_id`). It is on each
`publish-requests` row and on the definition, always the **current** value. `approve`
takes the value the administrator was shown and answers `409 DEFINITION_CHANGED` when
the stored definition no longer hashes to it; `PUBLISH_NOT_PENDING` is checked first.
The refusal deliberately does not return the new value: reading the new definition is
the only way to learn it. Reject needs no fingerprint.

It closes this: the author requests, the administrator opens the definition, the author
withdraws (which unfreezes the query), edits the SQL and requests again, and an approval
from the stale page would publish SQL nobody read.

It does not cover the chart's name or position, the query's name or description, or any
other chart on the query (each chart has its own fingerprint). **Known limit: a list's
items are not part of it.** Lists are shared and their creator, or an administrator, can
edit them, which changes what a published rule flags without changing the definition.
Closing that means versioning lists into the hash.

### `GET /queries/charts/{id}/definition`

The SQL and configuration behind a chart, read-only.

```json
{
  "chart": {"id": "...", "name": "Volume", "chart_type": "bar", "x_field": "day", "...": "..."},
  "query": {"id": "...", "name": "Daily volume", "description": null,
            "sql_text": "SELECT ...", "row_limit": 250, "poll_interval_ms": 3600000},
  "rules": [{"id": "...", "name": "Watched", "severity": "high", "enabled": true,
             "conditions": [{"column_name": "day", "operator": "in_list",
                             "value": null, "value2": null, "list_name": "Watchlist"}]}],
  "connection_name": "Payments",
  "owner_name": "Ada Lovelace",
  "read_only": true,
  "definition_fingerprint": "9b2f..."
}
```

Allowed for any signed-in user when the chart is `published`, and for the author
and administrators in every state (an approver reads a pending request's SQL here).
Anything else is `404`. `row_limit` and `poll_interval_ms` are the effective values.
It never carries the connection's id, host, port, database, username or
credentials, only its **name**; a list condition carries the list's **name**, never
its items. `read_only` is false for the author and administrators. There is no
write method on it.

### Shared alerts and personal dismissals

A query is **alert-visible** to its owner, to every administrator, and to everyone
once it has a published chart. `GET /flagged/summary` and
`GET /connections/{id}/flagged` cover alert-visible queries; per-query summary lines
and sections carry `shared` (true when the caller is not the owner).

Findings are stored once per query. A **dismissal belongs to the person who made
it**: `POST` and `DELETE /queries/{id}/flag-dismissals` act on the caller's own
dismissals, are allowed on any alert-visible query, and do not delete the stored
finding. Only fingerprints that are stored findings of that query are recorded; unknown
ones are ignored and not counted in `changed`, so a request cannot grow the table with
arbitrary hashes (restoring has no such limit, because it only removes). Every read (the summary, the flagged view, every poll) excludes the
caller's own dismissals and nobody else's, so a viewer clearing what they have read
hides nothing from the author, an administrator or another viewer. Restoring brings
a finding back at once, for the caller only. Clearing stored findings
(`DELETE /queries/{id}/flagged-rows`) and editing rules remain author and
administrator only. Unpublishing removes the query from the viewers' alerts.

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

## Lists

A list is a named, described set of values that any number of flag rules can test
a column against with `in_list` / `not_in_list`. Lists are shared: every signed-in
user can list, read and use every list. Only the creator or an admin can replace
or delete one; anyone else gets `403 FORBIDDEN`.

| Method | Path | Success |
|---|---|---|
| `GET` | `/lists` | 200, `ItemListSummary[]` sorted by name (no items) |
| `POST` | `/lists` | 201, `ItemListWriteResult` |
| `GET` | `/lists/{id}` | 200, `ItemListRead` |
| `PUT` | `/lists/{id}` | 200, `ItemListWriteResult` (replaces name, description and items) |
| `DELETE` | `/lists/{id}` | 204 |

Request body of `POST` and `PUT`: `{"name": string(1-200), "description": string(<=1000)|null, "items": string[]}`.

```json
{
  "id": "0f0c...", "name": "Blocked terminals", "description": "From chargebacks",
  "item_count": 2, "rule_count": 1, "created_by": "a1b2...",
  "created_at": "2026-09-30T09:12:44Z", "updated_at": "2026-09-30T09:12:44Z",
  "items": ["T-100", "T-200"],
  "received": 3, "kept": 2, "duplicates_dropped": 1
}
```

`ItemListSummary` is the object without `items`, `received`, `kept` and
`duplicates_dropped`; `ItemListRead` adds `items`; `ItemListWriteResult` adds all
four. `rule_count` is the number of distinct rules that use the list.

Items are trimmed; blanks are dropped; repeats are dropped by *match key*, keeping
the first spelling. `received` is how many were sent, `kept` how many were stored,
`duplicates_dropped` is `received - kept` (repeats plus blanks). Each item is at
most 500 characters (`422`), and a list holds at most `FAE_MAX_LIST_ITEMS` items
(default 20,000, counted before de-duplication, `422` with `detail.max_items`).
The list travels as one request body, capped by `FAE_MAX_REQUEST_BYTES` (1 MiB by
default; a body over it is refused before the item limit is checked), so a larger
list needs both settings raised together.

Matching: an item and a cell are compared by key.

* Numbers compare as numbers at full precision (`2`, `2.0` and `"2.00"` are one
  value; 31-digit ids stay distinct).
* Booleans bridge exactly as `in` does: a boolean cell matches an item spelled
  `true/t/yes/y/1` (or `false/f/no/n/0`), and a text cell that is a bool word
  matches an item that is a bool word. A digit never bridges to a word.
* Everything else compares trimmed and **case-insensitively**, and **without
  Unicode normalisation** (a composed and a decomposed accented letter differ).
  Unlike inline `in`, which is case-sensitive and does not trim the cell. So
  "same rows as `in`" holds for case-consistent, unpadded data.
* NULL cells match neither `in_list` nor `not_in_list`, so `not_in_list` is the
  complement over non-null cells.

List names are unique after NFC normalisation and case folding (`Éclair` and
`éclair` collide on every database). NUL characters in names, descriptions and
items are refused with `422`.

Saving a list's items invalidates the cached result of every query whose rules use
it; the next poll re-evaluates and `data_hash` changes if flags moved. No rule edit
is needed.

Known limit, shared with editing a rule: that invalidation happens in the process
that handled the save. With several workers (`WEB_CONCURRENCY` above 1) the other
workers converge after the cache TTL plus the stale-while-revalidate grace, and a
background refresh that was already running when the list was saved can store its
pre-edit flags for one TTL. Stored findings in the flagged view update when the
next run syncs them.

`DELETE` of a list a rule still reads (enabled or not) is `409 LIST_IN_USE`, with
the rules on queries the caller can see in `detail.rules` and a count of the rest
in `detail.hidden_rule_count` (queries are private to their owner, so another
analyst's are never named). Remove the list from those rules first; the database
also refuses the delete with a foreign key, so a delete racing a rule save cannot
leave a dangling reference. Deleting a query or its rules frees the list.

## Credentials

No response model in this API declares a `password` or `password_encrypted`
field. Credentials cannot appear in any response body, including
`GET /connections`.
