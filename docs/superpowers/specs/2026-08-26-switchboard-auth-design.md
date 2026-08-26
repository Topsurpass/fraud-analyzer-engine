# Switchboard: authentication, roles, and account management

**Status:** approved design, not yet implemented
**Date:** 2026-08-26

## Why

The app has no users. Every request is anonymous, every saved query belongs to
nobody, and `execution_log` records that a query ran against a production
payments database without recording who ran it. That is acceptable for one
person on a laptop and unacceptable the moment a second analyst exists.

This adds accounts, two roles, and ownership — and renames the product to
**Switchboard**.

**Outcome this moves:** an analyst can be given access to production payment
data without being given the ability to change what data sources exist, and
every query run against a customer database is attributable to a named person.
Measured by: `execution_log` rows carrying a `user_id` for 100% of runs after
the change, and a route-coverage test proving no endpoint is reachable without
authentication.

## Decisions taken

Asked and answered before design:

1. **Session lives in a Next.js BFF.** The browser holds an httpOnly cookie and
   talks only to Next.js, which proxies to the engine. The engine stops being
   internet-facing.
2. **Shared databases, private work.** Every analyst can query every connection
   the admin created. Queries, charts and dashboards belong to their author.
3. **First admin is created by CLI only.** Nothing reachable over the network
   can mint an admin.
4. **Accounts are deactivated, never deleted.** Work and attribution survive.
5. **Analysts publish their own charts; admins publish anyone's.** Publishing is
   the sharing mechanism the private-work model otherwise lacks, and it is
   self-service so it actually gets used.
6. **Publishing freezes the query behind it.** Whoever published may unpublish,
   which unfreezes it; an admin may always unpublish.

## Architecture

```
browser ──httpOnly cookie──> Next.js ──Bearer session──> engine ──> Postgres
         no JS access         proxy      server-side      private
```

### Sessions are opaque and server-side, not JWTs

A JWT stays valid until it expires. Under decision 4, a deactivated analyst
would keep working for the remainder of their token's life, and "deactivate"
that takes effect in thirty minutes is not deactivation.

The engine stores a `sessions` row and checks `users.is_active` on every
request. Deactivation and logout are immediate. It also removes refresh-token
machinery entirely, and there is no signing secret to rotate.

The cost is a database read per request. The engine already reads the database
on essentially every request, and the session lookup is a primary-key hit on a
small table.

### The engine verifies identity itself

Next.js forwards the session id; it does **not** forward a trusted `X-User-Id`.
If the engine trusted a header from the proxy, anything that reached the engine
directly could impersonate any user, and "the engine is on a private network"
would be the only thing standing between an attacker and every account. Private
networking is a second layer, never the only one.

## Data model

### New tables

**`users`**

| column | notes |
|---|---|
| `id` | uuid |
| `email` | unique, stored lowercased; the login identifier |
| `full_name` | shown as attribution on work |
| `password_hash` | argon2id |
| `role` | `admin` \| `analyst` |
| `is_active` | false blocks login and kills live sessions |
| `must_change_password` | set on admin-created accounts and on every reset |
| `temp_password_expires_at` | nullable; set with a temporary password, cleared when the user chooses their own |
| `failed_login_count`, `locked_until` | per-account lockout |
| `last_login_at` | for spotting dormant accounts |
| `created_by` | FK users, nullable (the first admin has no creator) |

Email is stored lowercased rather than relying on a case-insensitive collation,
because the app supports both SQLite and Postgres and `citext` exists on only
one of them.

**`sessions`** — opaque id (32 random bytes, stored hashed), `user_id`,
`created_at`, `expires_at`, `last_seen_at`, `ip`, `user_agent`.

The id is stored as a SHA-256 digest for the same reason passwords are hashed:
a leaked database dump otherwise hands over every live session. A fast digest
rather than argon2 — the value is already 256 bits of randomness, so it needs
no stretching, and argon2 on every request would be a self-inflicted denial of
service.

Expired rows are deleted opportunistically on login rather than by a scheduled
job, which keeps the table small without adding a background process that can
fail silently.

**`audit_log`** — `actor_id`, `action`, `target_type`, `target_id`, `detail`
(JSON), `created_at`. Records account created / deactivated / reactivated /
role changed / password reset, connection created / edited / deleted / paused,
and chart published / unpublished.

Distinct from `execution_log`, which records *queries running*. Conflating "who
changed the system" with "what ran against the target" would make both harder
to read.

### Changed tables

| table | change |
|---|---|
| `saved_queries` | `+ owner_id` (nullable FK users) |
| `dashboards` | `+ owner_id` (nullable FK users) |
| `connections` | `+ created_by` (nullable FK users) |
| `execution_log` | `+ user_id` (nullable FK users) |
| `query_charts` | `+ is_public`, `+ published_by`, `+ published_at` |

Whether a query is frozen is *derived* — it has at least one published chart —
rather than stored as a flag on the query. A stored flag would be a second
source of truth that could disagree with the charts it describes, and the
disagreement would surface as either an un-editable query nobody can explain or
a published chart that silently drifts.

`query_charts` and `flag_rules` inherit ownership through their query and need
no owner column. Adding one would create two sources of truth that could
disagree.

**All owner columns are nullable, deliberately.** Existing rows predate users,
and there is no admin at migration time to attribute them to. A NULL owner
means "unowned": visible to admins only, never to analysts. `fae create-admin`
offers to claim unowned resources, so the current queries neither vanish nor
become visible to every new analyst.

Foreign keys to `users` are `ON DELETE RESTRICT`, which enforces decision 4 at
the schema level rather than by convention.

## Roles

**Role changes take effect immediately.** Because the engine reads the user row
on every request, promoting or demoting someone does not require them to log
out. This falls out of the opaque-session choice; under JWTs a demoted admin
would keep admin powers until their token expired.

**The last admin cannot be locked out.** Deactivating, demoting, or otherwise
removing admin access from the only remaining active admin is refused at the
service layer, not merely hidden in the UI. Without that guard a single
mis-click leaves an installation with no one able to create accounts or manage
connections, recoverable only by someone with shell access — and it is exactly
the kind of mistake made while tidying up an account list.

| capability | analyst | admin |
|---|---|---|
| Log in, change own password | ✅ | ✅ |
| List connections (name, type, status) | ✅ | ✅ |
| Read connection credentials | ❌ | ❌ |
| Create / edit / delete / pause connections | ❌ | ✅ |
| Create queries, charts, dashboards, flag rules | ✅ own | ✅ |
| Read / edit / delete others' work | ❌ | ✅ |
| See published charts | ✅ | ✅ |
| Publish / unpublish own chart | ✅ | ✅ |
| Publish / unpublish anyone's chart | ❌ | ✅ |
| Edit a frozen query | ❌ | ✅ |
| Create accounts, deactivate, reset password, change role | ❌ | ✅ |
| Audit log; execution log across all users | ❌ | ✅ |

**Nobody reads credentials, including admins.** `ConnectionRead` does not
declare `password`, so credentials cannot leak through the response model even
by mistake. That property exists today and is preserved.

## Enforcement

Authorisation lives in the engine. Hiding admin navigation from analysts is a
courtesy to the reader, not a control — an analyst who crafts the request
directly must be refused by the server.

Three layers:

1. **`require_user`** — a FastAPI dependency resolving the session to a live,
   active user. Applied at the router level so it is inherited, not
   remembered per endpoint.
2. **`require_admin`** — the same, plus a role check. Every admin-only router.
3. **Ownership filtering in the service layer** — list endpoints filter by
   `owner_id` for analysts; fetch endpoints refuse a resource the caller does
   not own. Filtering only in the router would leave the service functions
   safe to call wrongly from somewhere else later.

**A route-coverage test** enumerates every route registered on the app and
fails if any lacks an auth dependency, with an explicit allowlist for the
genuinely public ones (`/health`, `/auth/login`). A future endpoint added
without auth breaks the build instead of shipping open. This is the single
highest-value test in the change: it is the one that stays correct as the app
grows.

### Analyst SQL is now a security boundary

Analysts write arbitrary SQL against databases an admin connected. Read-only is
already enforced three ways — `default_transaction_read_only=on`, the statement
guard in `app.security.sql_guard`, and read-only connection opens. Those stop
being defence in depth against an operator's own mistake and become the control
that prevents an analyst writing to production. No change is required; the
significance changes, and the tests covering it become critical-path.

## Publishing

`is_public` on `query_charts`. An analyst may publish a chart they own; an admin
may publish anyone's. A published chart is readable by every signed-in user and
exposes:

- the chart spec and the query's **result rows** (without them it cannot render)
- the query's name and the author's name

It does **not** expose the SQL text, sibling unpublished charts, flag rules, or
any edit right. The owning analyst can see that their chart is published, which
matters for trust: work should not become visible to colleagues without its
author knowing.

### Publishing freezes the query

If a chart is published and the analyst then edits the query's SQL, they change
what every colleague sees with no review. That is a privilege escalation wearing
ordinary clothes, and it exists whether the analyst published the chart or an
admin did.

**A query with at least one published chart is frozen.** Frozen means `sql_text`
and `row_limit` cannot change, and neither can the published chart's own field
mapping — all three change what viewers see. Everything else stays editable:
the query's name, description and poll interval, its unpublished sibling
charts, and its flag rules, none of which alter the data being shown. A frozen
query and a published chart also cannot be deleted while published.

An admin can edit a frozen query. Nobody else can, including its owner.

This replaces an earlier rule in this design that auto-unpublished a chart when
its SQL changed. The freeze is strictly better: it makes the drift *impossible*
rather than *corrected after the fact*, and it fails in the direction of
refusing an edit rather than silently retracting something a colleague was
relying on. One rule fewer, and the failure mode is a clear error message
instead of a chart quietly vanishing from someone else's board.

**Unfreezing is unpublishing, and whoever published may do it.** An analyst can
unpublish a chart they published themselves, edit the query, and republish;
viewers see the chart leave the board and return changed, which is visible
rather than silent — and silent change was the actual risk. A chart an *admin*
published can only be unpublished by an admin, so an admin retains a genuine
"freeze this" power over anyone's work.

The refusal message names the way out rather than only the rule: an analyst
blocked from editing is told which of their charts is published and that
unpublishing it will unfreeze the query.

## Everything else

- **Passwords:** argon2id. Minimum 12 characters, rejected against a bundled
  common-password list.
- **Login responses never reveal whether an email exists.** Same message, same
  timing, for unknown email and wrong password.
- **Per-account lockout** after repeated failures, on top of the existing IP
  rate limiter, which alone does not stop a distributed attempt on one account.
- **Sessions:** 12h absolute, 8h idle, both configurable. `SameSite=Lax` on the
  cookie, plus an `X-Switchboard-Request` header required on every mutating
  request. A cross-site form post cannot set a custom header, and a
  cross-origin `fetch` that tries is stopped by preflight — so the pair covers
  CSRF without a token round-trip.
- **No password reset by email.** There is no mail infrastructure and adding one
  is a separate project. Analysts contact an admin; a locked-out admin uses
  `fae reset-password`.
- **`dev-seed.mjs` and `smoke.mjs`** both need a login step; the smoke lane
  otherwise reports the whole app as broken the moment auth lands.

### Temporary passwords and the first login

An admin never types an analyst's password and never learns their working one.

**Creating an account.** The admin supplies name, email and role. The system
generates the temporary password — the admin does not choose it, because a
human-chosen "temp" password is reliably weak and reliably reused across every
account that person creates. It is displayed exactly once, with a copy control
and an unambiguous warning that it will not be shown again, and the admin
conveys it out of band. It is stored only as an argon2id hash, like any other
password, so it cannot be read back from the database or the API.

**It expires in 72 hours.** An unclaimed credential sitting valid forever in
somebody's chat history is a standing liability. After it lapses the admin
issues a new one, which is the same operation as a reset.

**The first login is not a normal session.** Authenticating with a temporary
password creates a real session, but `require_user` refuses every route except
"change my own password" with a distinct error code, and the app routes
straight to the change screen. The restriction is enforced by the engine, not
by the frontend simply declining to show anything else — a restricted session
that can call the API freely is not restricted.

**Changing it clears the flag and invalidates every other session** for that
user. If the temporary password was intercepted in transit and used by someone
else first, the real owner changing it ends the intruder's session rather than
running alongside it.

**Resetting** is the same flow: a new temporary password, `must_change_password`
set, and all of that user's sessions killed immediately — so a reset performed
because an account is suspected compromised actually ejects whoever is in it.

### CLI

```
uv run fae create-admin        # first admin; offers to claim unowned resources
uv run fae reset-password      # lockout recovery
uv run fae list-users
```

Runs identically under Docker (`docker compose exec analyzer uv run fae ...`)
and without it (`cd services/analyzer && uv run fae ...`), because it loads the
same `FAE_`-prefixed settings object the server does and therefore always
targets the database the server is using.

It prints the target database before writing and refuses to run if the `users`
table does not exist. Without that guard, running it from the wrong directory
would create an admin in the SQLite fallback while the real Postgres stayed
empty — and the only symptom would be "invalid credentials" at a login page,
with nothing anywhere explaining why.

## The rename

**Fraud Analyzer → Switchboard.** A payment switch, and a board of charts.

Identity: a 3×3 grid with one cell lit — a switch panel and a dashboard at
once, with the lit cell reading as the flagged one. It resolves at 16px, works
in a single colour, and needs no gradient.

Scope of the rename: user-facing strings, page titles, favicon, README
headings, and the login page. **Not** renamed: the repository directories,
Python package, Docker image names, or the `FAE_` environment prefix. Renaming
those touches deployment, `.env` files and the compose setup for no user-visible
gain, and is reversible later if wanted.

## Testing

**Gate lane (deterministic, free, every commit):**

- Route coverage: every registered route has an auth dependency or is
  explicitly allowlisted.
- Role matrix: for each admin-only endpoint, an analyst session receives 403.
- Ownership: analyst A cannot read, edit or delete analyst B's query, chart,
  dashboard or flag rules — by id, not merely by absence from a list.
- Deactivation kills live sessions immediately.
- A role change takes effect on the next request, without re-login.
- The last active admin cannot be deactivated or demoted, by either route.
- Login does not distinguish unknown email from wrong password.
- Lockout engages and releases.
- Password hashing round-trips; the hash never appears in any response body.
- Publishing: an analyst can publish a chart they own and cannot publish
  someone else's; an admin can publish anyone's; a published chart is visible
  to every user while its SQL text is not.
- Freezing: a query with a published chart refuses an SQL or row-limit edit
  from its owner and accepts one from an admin; the published chart's field
  mapping is frozen while its unpublished siblings are not; neither the chart
  nor the query can be deleted while published.
- Unfreezing: an analyst can unpublish what they published and then edit; an
  analyst cannot unpublish what an admin published; an admin can unpublish
  anything.
- Temporary passwords: a session carrying `must_change_password` is refused
  every route but the password change, by the engine rather than the UI;
  changing it clears the flag and kills that user's other sessions; an expired
  temporary password no longer authenticates.
- Migration: upgrade and downgrade; existing rows land with NULL owners.
- Frontend: the login page, the redirect for an unauthenticated visitor, admin
  navigation hidden from analysts, the forced password change on first login.

**No eval suite.** Every decision here is same-input-same-output; per the
latent/deterministic split in CLAUDE.md this is gate-test work throughout.

## Sequencing

The engine leads, because the frontend types against the regenerated
`contracts/openapi.json`.

1. `users` + `sessions` models, migration, argon2 hashing, the CLI.
2. `/auth/login`, `/auth/logout`, `/auth/me`, and the two dependencies.
3. Ownership columns and service-layer filtering, endpoint by endpoint.
4. Admin user-management endpoints and the audit log.
5. Publishing, and the freeze it implies.
6. Next.js proxy, cookie handling, login page, route guards.
7. Admin UI: user list, create, deactivate, reset.
8. Rename and identity.

Steps 1–2 are not independently useful — the app is unusable between them and
step 3 — so they land together on a branch rather than one at a time on master.

## Not building

Named so they are decisions rather than omissions: SSO/OAuth, 2FA, email
verification or reset, per-analyst database grants, analyst-to-analyst sharing
(publishing covers the need), public unauthenticated links, and password
expiry.
