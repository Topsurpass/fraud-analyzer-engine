# Shared publishing: design and acceptance criteria

Branch `feat/shared-publishing` in both repos. This document is the contract the
engine and the dashboard are built against, and the rubric the finished work is
judged by. It is frozen once building starts: change it in a commit of its own,
with the reason, never to make a failing check pass.

Four changes:

1. Publishing needs an administrator's approval when an analyst asks.
2. Everyone who can see a published chart also sees its alerts, and may dismiss
   them for themselves.
3. Cards that get flagged move to the top, smoothly.
4. Everyone who can see a published chart can read the query and configuration
   behind it, and cannot change any of it.

## Vocabulary

- **Author**: the user who owns the query behind a chart.
- **Viewer**: any signed-in user who is not the author and not an administrator.
- **Published query**: a query with at least one chart whose `publish_status` is
  `published`.

## 1. Approval before publishing

### States

`QueryChartRead` gains `publish_status`: `"private" | "pending" | "published"`.

| Status | Meaning | `is_public` |
| --- | --- | --- |
| `private` | Default. Only the author and administrators see it. | false |
| `pending` | The author asked; an administrator has not decided. | false |
| `published` | Visible to everyone signed in. | true |

`is_public` keeps its meaning (true only when `published`), so everything that
already reads it keeps working. `publish_status` is derived, never stored twice.

`QueryChartRead` also gains, all nullable:

- `publish_requested_at`: ISO time, set while `pending`.
- `publish_rejection`: `{ reason: string | null, rejected_at: string,
  rejected_by_name: string }`, set after a rejection until the author asks again
  or withdraws. A rejected chart is `private`.
- `published_by_name`: display name of the author, for viewers.

### Endpoints (all under `/queries/charts/...`, literal routes before `/{chart_id}`)

| Method and path | Who | Effect |
| --- | --- | --- |
| `POST /{chart_id}/publish` | author, or admin on any chart | Author who is **not** an admin: becomes `pending` and `publish_rejection` clears. Admin: becomes `published` at once, as today. Already `pending` or `published`: no change, returns the chart. |
| `POST /{chart_id}/publish/cancel` | author, or admin | `pending` becomes `private`; also dismisses a rejection notice. On `published`: no change (use unpublish). |
| `POST /{chart_id}/unpublish` | unchanged | Unchanged rule: the author who published may retract, an admin always. |
| `GET /publish-requests` | admin only (`FORBIDDEN` otherwise) | Every `pending` chart, oldest first: `PublishRequestRead`. |
| `POST /{chart_id}/publish/approve` | admin only | `pending` becomes `published`. `published_by` stays the requesting author. Anything not `pending`: `409`, code `PUBLISH_NOT_PENDING`. |
| `POST /{chart_id}/publish/reject` | admin only | Body `{ "reason": string \| null }`, at most 500 characters. `pending` becomes `private` with `publish_rejection` set. Anything not `pending`: `409`, `PUBLISH_NOT_PENDING`. |

`PublishRequestRead`:

```json
{
  "chart": { "...QueryChartRead..." },
  "query_id": "...", "query_name": "...",
  "connection_id": "...", "connection_name": "...",
  "requested_by": { "id": "...", "full_name": "...", "email": "..." },
  "requested_at": "ISO"
}
```

Every transition writes an audit-log entry: `chart.publish_requested`,
`chart.publish_approved`, `chart.publish_rejected`, `chart.publish_cancelled`,
and the existing publish and unpublish events.

### Freeze

A query with a `pending` chart is frozen for non-admins exactly like one with a
`published` chart (`QUERY_FROZEN`), with a message that says the request is
waiting and that withdrawing it unfreezes the query. Without this an author could
change the SQL after asking and before the approver looked. An admin may still
edit; editing a query that has a `pending` chart does not change its status.

### Acceptance (engine)

- An analyst's publish never produces `published`. Only an administrator's action
  does, whether publishing or approving.
- Approve and reject are administrator-only, and refuse non-`pending` charts.
- A non-admin cannot see another author's pending request (`404`, as everywhere).
- `GET /publish-requests` is `FORBIDDEN` for a non-admin and lists only `pending`.
- Pending freezes the query for the author; cancel unfreezes it.
- Existing admin publish and unpublish behaviour is unchanged.
- Every transition is audited with the actor.

### Acceptance (dashboard)

- A non-admin's card menu says **Request publishing**. After asking, the card
  carries an **Awaiting approval** badge and the menu offers **Withdraw request**.
- After a rejection the card shows the reason and the menu offers **Request
  again**.
- An admin's menu still says **Publish** and publishes at once.
- `/approvals` (admin only; a non-admin gets a plain "Administrators only" page,
  not a crash) lists pending requests with requester, chart, query name and
  connection, opens the read-only definition (section 4) so the SQL is reviewed
  before approving, and offers **Approve** and **Reject** (with an optional
  reason). The list updates without a reload after a decision.
- Admins see an **Approvals** link in the rail with the pending count, and the
  bell mentions waiting requests with a link. Nothing appears for non-admins.

## 2. Shared alerts, personal dismissals

### Visibility

A query is **alert-visible** to a user if they own it, are an administrator, or
the query is published. Flagged findings are stored once per query as today and
now also reach everyone who can see a published chart on it.

### Dismissals become personal

`flag_dismissals` gains `user_id` (not null, unique with `query_id` and
`row_fingerprint`). Migration backfills each existing dismissal to the owner of
its query, or to the first administrator when the query has no owner, and drops a
row that has neither.

- Dismissing records the dismissal for the caller only and **does not delete** the
  stored finding. `sync()` stores every current match, dismissed by anyone or not.
- Every read filters by the caller's own dismissals: `GET /flagged/summary`,
  `GET /connections/{id}/flagged`, and every poll variant (`apply_dismissals`
  receives the caller's set). Because `apply_dismissals` already mixes the
  dismissal state into `data_hash`, the rendered-response cache stays correct per
  user.
- `POST` and `DELETE /queries/{id}/flag-dismissals` are allowed on any
  alert-visible query, and act on the caller's dismissals only.
- `DELETE /queries/{id}/flagged-rows` (clear stored findings) stays author and
  admin only.

### Response additions (additive, optional for older clients)

- `FlaggedQueryTally.shared: boolean`: true when the caller is not the owner.
- `FlaggedQuery` (in `ConnectionFlagged`): `shared: boolean`,
  `owner_name: string | null`.

### Acceptance (engine)

- A viewer's summary and flagged view include findings of published queries and
  exclude unpublished ones belonging to others.
- A viewer's dismissal hides that finding for the viewer only. The author, an
  admin and every other viewer still see it, and counts agree per user.
- The author's dismissal no longer hides it from others.
- Dismissing does not delete stored findings. Restoring brings the finding back
  for that user only.
- A viewer cannot clear stored findings or edit rules.
- The migration preserves every existing dismissal for the query's owner.
- Unpublishing removes the query from the viewers' alerts.

### Acceptance (dashboard)

- A viewer's bell and rail counts include alerts from published queries and fall
  when the viewer dismisses.
- The flagged page labels a shared section **Shared by <owner>** and lets the
  viewer dismiss and restore rows, without any control that edits the query or
  its rules.
- A published card shows its flag marks, strip and **Review N flagged rows** link
  to a viewer too.

## 3. Flagged cards move to the top

Applies to the connection page, every dashboard page and the published section.

### Order

A card is **flagged** when its chart's current `flagged_count` is above zero
(dismissals the user made already excluded). Sort, in this order:

1. flagged before unflagged;
2. among flagged, **most recently newly flagged first**: the time the card's
   flagged count last rose (or first appeared), from a poll;
3. then higher worst severity, then higher count;
4. then the original order. The sort is stable, so ties never shuffle.

### Motion

- When a poll changes the order, cards move to their new places with a FLIP
  animation (measure, apply the new order, animate from the old rectangle):
  between 250 ms and 450 ms, an ease-out curve, transform only.
- The card that moved up gets the existing amber "changed" highlight for a beat.
- `prefers-reduced-motion: reduce`: reorder without animation.
- No layout thrash: one measure pass, one write pass, per reorder.

### Never move what the user is using

The order is **held**, not applied, while a card menu or dialog is open, a card is
expanded, or the pointer is pressed. It applies the moment that ends. A card must
never jump out from under a click.

### Acceptance (dashboard)

- Unit tests prove the comparator for every rule and for stability.
- A browser check shows: a card that becomes flagged rises to the top and animates
  (its `transform` is non-identity mid-flight and identity after); two polls with
  the same flags change nothing; with a menu open the order is held and applies on
  close; with reduced motion there is no animation.
- Dismissing a card's last flagged row returns it to its place smoothly.

## 4. Read-only definition for published charts

`GET /queries/charts/{chart_id}/definition` returns `ChartDefinitionRead`:

```json
{
  "chart": { "...QueryChartRead..." },
  "query": { "id": "...", "name": "...", "description": "...",
             "sql_text": "...", "row_limit": 1000, "poll_interval_ms": 3600000 },
  "rules": [ { "id": "...", "name": "...", "severity": "high", "enabled": true,
               "conditions": [ { "column_name": "...", "operator": "...",
                                 "value": "...", "value2": null,
                                 "list_name": null } ] } ],
  "connection_name": "...",
  "owner_name": "...",
  "read_only": true
}
```

- Allowed for any signed-in user when the chart is `published`; for the author and
  administrators in any state (administrators need it to review a `pending`
  request). Anything else is `404`.
- Never contains connection credentials, host, port, database or the connection id.
  A list condition carries the list's **name** in `list_name`, never its items.
- `read_only` is true unless the caller is the author or an administrator.
- Read only means there is no write endpoint on it, and the dashboard shows no
  control that edits.

### Acceptance (engine)

- A viewer gets the definition of a published chart and `404` for a private or
  pending one that is not theirs.
- The body never contains the connection's host, database, username or id.
- List conditions expose the list name and not its items.
- The author and admins get it in every state, and `read_only` is false for them.

### Acceptance (dashboard)

- Published cards show **View definition** in a menu for viewers (the author's
  full menu is unchanged). It opens a dialog: the SQL in monospace with **Copy**,
  poll interval, row limit, the chart's field mapping, the rules in plain words,
  the owner's name, and a banner "Read-only. This belongs to <owner>."
- The dialog has no input that edits anything. Escape closes it, and focus returns
  to the trigger.
- The same dialog is used on `/approvals` so an admin reads the SQL before approving.

## Not in scope

Notifications by email or push, per-user publish targets (publish to a team),
commenting on a request, and editing a published chart from the viewer's side.
