# Where things live

One folder per feature. Anything an operator would want to add or remove is a
plain file in `app/policy/`. Everything else you find by asking "which feature
is this?".

## "I want to..." (start here)

| I want to... | Edit this | Then |
|---|---|---|
| Allow or block a SQL keyword, function, or clause | `app/policy/sql_allowlist.py` | Add a case to `tests/test_sql_guard.py`. Unblocking something? Delete or invert its existing rejection case there first (for example `SELECT sleep(30)`), or that test fails |
| Add or remove a chart type | `app/policy/chart_types.py`: the enum member and its `REQUIRED_FIELDS` entry | Frontend must learn to draw it. No migration to add. Removing one also breaks old migrations that import it (see the file header) |
| Add a flag-rule comparison (a new operator) | `app/policy/flag_rules.py`, then `app/features/flag_rules/engine.py` (`evaluate_condition`) | Add a row to `_OPERATOR_TRUE_CASES` in `tests/test_policy.py`; it fails until you do |
| Add a flag severity | `app/policy/flag_rules.py` (member and rank in `SEVERITY_ORDER`) | Nothing else; every ranking in the code reads `SEVERITY_ORDER`, and `tests/test_policy.py` checks it |
| Add a list operator (a comparison against a named list) | `app/policy/flag_rules.py` (member and `LIST_OPERATORS`), then `app/features/flag_rules/engine.py` (`evaluate_condition`, reads `ConditionSpec.members`) | Add a row to `_OPERATOR_TRUE_CASES` in `tests/test_policy.py` and cases to `tests/test_list_matching.py`. How a cell becomes a match key is `list_key` in `app/features/lists/matching.py` |
| Change what a rule matches on, or how conditions combine | `app/features/flag_rules/engine.py` | `tests/test_flagging.py` |
| Add or edit a flag rule, chart, or saved query itself | It is data, not code: use the API (`PUT /queries/{id}/flag-rules`, `PUT /queries/{id}/charts`, `POST /connections/{id}/queries`) or the dashboard | |
| Change a limit or timeout (row limit, query timeout, poll interval, rate limit, cache size) | `app/config.py` (default) or the matching `FAE_*` env var | `.env.example` lists them |
| Add an endpoint to an existing feature | `app/features/<feature>/router.py`, logic in `service.py`, shapes in `schemas.py` | `python ../../scripts/export_openapi.py`; `tests/test_route_coverage.py` checks auth |
| Add a database column or table | `app/features/<feature>/models.py`, plus a migration in `alembic/versions/` | `tests/test_migrations.py` fails if they disagree |
| Add a new feature | New folder under `app/features/`, see "Adding a feature" | `tests/test_structure.py` |
| Support another database type (Postgres, MySQL, SQLite today) | `app/enums.py` (`DbType`), `app/db/target_registry.py` | `app/policy/sql_allowlist.py` for its dangerous functions |
| Change who may do what (admin vs analyst) | `app/security/deps.py` on the route; `UserRole` in `app/enums.py` | `tests/test_role_enforcement.py` |

## The map

```
services/analyzer/
  app/
    main.py            builds the app, wires middleware and every router
    config.py          every setting and its default
    errors.py          error codes and the HTTP status each maps to
    enums.py           small shared enums (DbType, UserRole, SslMode, ...)
    types.py           shared Pydantic types (UtcDatetime)
    cli.py             `fae` command line (create admin user, etc.)
    observability.py   request ids and logging
    ratelimit.py       rate limit and request size middleware

    policy/            THE EDITABLE KNOBS. Plain data, one concern per file.
      sql_allowlist.py   forbidden keywords/functions, allowed statement types
      chart_types.py     ChartType
      flag_rules.py      FlagOperator, FlagSeverity, SEVERITY_ORDER

    features/          one folder per product feature
      connections/     the databases you analyse
      queries/         saved SELECTs: run, poll, cache, schedule
      charts/          how a query's result is drawn; publishing (by request, with admin approval) and the read-only definition
      flag_rules/      rules that mark rows, and the reviewed queue
      dashboards/      boards that place charts
      lists/           named, described lists of values that flag rules test against
      auth/            login, logout, sessions, password change
      users/           user accounts and the audit-log endpoint
      audit/           the audit log table and writer

    security/          SQL guard machinery, password hashing, encryption, auth deps
    db/                engines, migrations runner, base classes, model registry
  alembic/             migrations (history; do not edit old ones)
  tests/               one test file per concern; test_policy.py and test_structure.py guard this layout
```

### Inside a feature folder

Same file names everywhere, so you know what to open:

| File | Holds |
|---|---|
| `models.py` | Database tables (SQLAlchemy) |
| `schemas.py` | Request and response shapes (Pydantic) |
| `service.py` | The logic. No HTTP in here |
| `router.py` | Routes, auth dependencies, status codes. Thin |

Extra files appear only where a feature has a distinct second job:

| Feature | Extra file | Holds |
|---|---|---|
| `connections/` | `introspection.py`, `introspection_router.py` | Table and column listing for a target database |
| `queries/` | `execution.py` | Runs a saved query against its target, builds the result payload |
| `queries/` | `polling.py` | The shared run/poll core used by every read path |
| `queries/` | `result_cache.py`, `rendered_cache.py`, `sizing.py` | Result caches and payload sizing |
| `queries/` | `refresher.py`, `scheduler.py` | Background refresh and the timer that runs queries |
| `charts/` | `fingerprint.py` | The hash an approval is bound to (SQL, limits, mapping, rules). A list's items are deliberately not in it: a known limit, see `contracts/analyzer-api.md` |
| `flag_rules/` | `engine.py` | Pure evaluation: rows and rules in, flags out |
| `flag_rules/` | `flagged_rows.py`, `dismissals.py` | The stored queue of matches, and each person's own dismissals of it |
| `lists/` | `matching.py` | Pure: how an item or a cell becomes a match key, and the member-set cache (keyed by list id and `ItemList.version`). `flag_rules/engine.py` imports it, never the reverse |
| `auth/` | `sessions.py` | Session create, validate, expire |

### Dependency direction

`router` -> `service` -> `models`, and `schemas` beside them. A feature may use
another feature's `service`, `schemas` or `models`, never its `router`
(`tests/test_structure.py` enforces that). Shared code goes in `app/` top level,
`app/security/`, `app/db/` or `app/policy/`, not in a feature.

## Adding a feature

1. `mkdir app/features/<name>` with `__init__.py`.
2. Add `models.py` if it has tables, and add one import line for it to
   `app/db/registry.py` (otherwise its tables silently vanish from `create_all`
   and alembic; `tests/test_structure.py` fails if you forget).
3. Add `schemas.py`, `service.py`, `router.py`.
4. In `app/main.py`, import the router and `app.include_router(...)` it.
5. Add its folder to `STRUCTURE.md` (a test checks the name appears here) and a
   migration in `alembic/versions/`. Copy `dashboards/` as the smallest complete
   example (model, schemas, service, router, owner scoping).
   Put its API test file's name in `_INTEGRATION_MODULES` in `tests/conftest.py`,
   or it runs in the fast gate lane. `tests/test_route_coverage.py` will fail
   until every new route sits behind `require_user` or `require_admin`.
6. `python ../../scripts/export_openapi.py` to refresh `contracts/openapi.json`.

## Removing a feature

Delete the folder, its line in `app/db/registry.py`, its `include_router` line
in `app/main.py`, add a migration that drops its tables, regenerate the OpenAPI
contract, and delete its tests and its entry in `_INTEGRATION_MODULES`
(`tests/conftest.py`). Then grep for `app.features.<name>` and for its model
class names: other features reference models by relationship, not only by
import. Dashboards, for example, are also referenced by `DashboardItem`
relationships in `queries/models.py` and `charts/models.py`, and by
`app/cli.py` (owner reassignment). Anything left is a dependency to cut first.

## Editing the SQL policy safely

`app/policy/sql_allowlist.py` is the only file you need. Two rules it enforces
at import so a typo cannot fail open:

* keywords and statement types are **UPPER CASE**;
* function and bare-name entries are **lower case**.

A wrong-case entry matches nothing, so the service refuses to start instead.
Loosening the policy (removing a function, allowing another statement type) is a
security decision: the comments in that file say what each group protects.

## Lists: known limit

Saving a list invalidates `result_cache` in the process that handled the request,
the same as saving a rule. With more than one worker, the others converge after
the cache TTL plus the stale grace, and a background refresh already in flight can
re-store pre-edit flags for one TTL. There is deliberately no cross-process
machinery for this. The member-set cache is safe regardless: it keys on
`ItemList.version`, which every save increments.
