"""What the SQL guard lets through, and what it refuses.

EDIT THIS FILE to change which SQL is permitted. ``app/security/sql_guard.py``
holds the checking machinery and should rarely need touching; every list an
operator would want to adjust lives here.

What to edit for which change:

* Block another statement keyword (``MERGE``, ``CALL``): add it to
  ``FORBIDDEN_KEYWORDS``, upper case.
* Block another bare word that ``sqlparse`` types as a name: ``FORBIDDEN_NAMES``,
  lower case.
* Block another function: add it, lower case, to the group for its engine
  (``POSTGRES_FUNCTIONS``, ``MYSQL_FUNCTIONS``, ``SQLITE_FUNCTIONS``).
* Allow a function that is currently blocked: remove it from its group. Read
  the comment above the group first; several entries are the only barrier
  against filesystem reads or turning off the read-only session.
* Allow more than ``SELECT`` as the leading statement: ``ALLOWED_STATEMENT_TYPES``.
  Think hard first. Everything else in the guard assumes reads only.
* Block another locking clause: ``FORBIDDEN_LOCKING_WORDS``.

``validate_policy()`` runs at import and on every test run. A lower-case keyword
or an upper-case function would otherwise be accepted silently and never match
anything, which fails open, so a malformed entry stops the service instead.
Every entry's rejection is pinned by ``tests/test_sql_guard.py``; add a case
there for anything you add.
"""

from __future__ import annotations

#: Reserved words that may never appear anywhere in a read-only query.
#:
#: Each entry is ANSI-reserved, so it cannot be a bare column name. ``INTO``
#: covers ``SELECT ... INTO table``, MySQL's ``INTO OUTFILE`` and ``INTO
#: DUMPFILE``. ``UPDATE`` also blocks ``SELECT ... FOR UPDATE``, which is a
#: locking read and has no place in an analytics query.
FORBIDDEN_KEYWORDS: frozenset[str] = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "ALTER",
        "CREATE",
        "TRUNCATE",
        "GRANT",
        "REVOKE",
        "INTO",
        "ATTACH",
        "DETACH",
        "EXEC",
        "EXECUTE",
        "VACUUM",
        "REINDEX",
        "UPSERT",
    }
)

#: Words ``sqlparse`` types as a plain name rather than a keyword, but which are
#: never legitimate in a read query.
FORBIDDEN_NAMES: frozenset[str] = frozenset({"outfile", "dumpfile", "pragma"})

#: Functions that read the filesystem, open a network connection, mutate
#: server state, or burn CPU. A statement can be a flawless SELECT and still
#: exfiltrate or write through one of these, so statement type alone is not
#: enough.
#:
#: ``set_config`` is the most important entry. The read-only guarantee has
#: three layers, and the middle one is a libpq connect option
#: (``default_transaction_read_only=on``, see
#: ``target_registry.postgres_connect_args``). ``SELECT set_config(
#: 'default_transaction_read_only', 'off', false)`` turns that layer off for
#: the life of the pooled connection, so it survives into later requests that
#: reuse the same handle. That is a privilege escalation, not a coverage gap.
#:
#: Deliberately absent: ``generate_series`` and ``repeat``. Both are ordinary
#: in analytics and both are bounded by the statement timeout and the result
#: byte budget rather than by a blocklist.
POSTGRES_FUNCTIONS: frozenset[str] = frozenset(
    {
        "set_config",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "pg_ls_logdir",
        "pg_ls_waldir",
        "pg_ls_tmpdir",
        "pg_ls_archive_statusdir",
        "pg_logdir_ls",
        "pg_sleep",
        "pg_sleep_for",
        "pg_sleep_until",
        "pg_advisory_lock",
        "pg_advisory_xact_lock",
        "lo_import",
        "lo_export",
        "lo_get",
        "lo_put",
        "lo_from_bytea",
        "lo_unlink",
        "lo_open",
        "lo_read",
        "lo_write",
        "loread",
        "lowrite",
        "dblink",
        "dblink_exec",
        "dblink_connect",
        "dblink_connect_u",
        "dblink_send_query",
        "dblink_fetch",
        "dblink_open",
        "dblink_close",
        "query_to_xml",
        "query_to_xmlschema",
        "query_to_xml_and_xmlschema",
        "nextval",
        "setval",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_promote",
        "pg_switch_wal",
        "pg_switch_xlog",
        "pg_create_restore_point",
        "pg_start_backup",
        "pg_stop_backup",
        "pg_backup_start",
        "pg_backup_stop",
        "pg_drop_replication_slot",
        "pg_create_physical_replication_slot",
        "pg_create_logical_replication_slot",
        "pg_stat_reset",
        "pg_stat_reset_shared",
        "pg_notify",
    }
)

#: MySQL-only. Not reachable through PostgreSQL's field-notation call syntax,
#: because MySQL has no such syntax.
MYSQL_FUNCTIONS: frozenset[str] = frozenset(
    {
        "load_file",
        "benchmark",
        "sleep",
        "get_lock",
        "release_lock",
        "release_all_locks",
        "master_pos_wait",
        "source_pos_wait",
        "sys_exec",
        "sys_eval",
    }
)

#: SQLite-only, same reasoning as the MySQL group.
SQLITE_FUNCTIONS: frozenset[str] = frozenset(
    {
        "readfile",
        "writefile",
        "load_extension",
        "edit",
        "fts3_tokenizer",
        "zipfile",
        "sqlar_compress",
        "sqlar_uncompress",
    }
)

FORBIDDEN_FUNCTIONS: frozenset[str] = (
    POSTGRES_FUNCTIONS | MYSQL_FUNCTIONS | SQLITE_FUNCTIONS
)

#: Statement types allowed to lead a query, as reported by ``sqlparse``.
#: ``sqlparse`` resolves a CTE to the command after it, so ``WITH x AS (...)
#: DELETE ...`` reports ``DELETE`` and is refused. It also means ``PRAGMA``,
#: ``COPY``, ``SET``, ``EXPLAIN`` and ``VALUES`` are refused without being listed.
ALLOWED_STATEMENT_TYPES: frozenset[str] = frozenset({"SELECT"})

#: Words that turn a SELECT into a locking read when they follow ``FOR``
#: (``FOR SHARE``, ``FOR KEY SHARE``). Cannot go on ``FORBIDDEN_KEYWORDS``:
#: ``share`` is also an ordinary column name, so the guard anchors on the
#: ``FOR ... <word>`` sequence instead. ``FOR UPDATE`` is caught by ``UPDATE``
#: on the keyword list.
FORBIDDEN_LOCKING_WORDS: frozenset[str] = frozenset({"SHARE"})


def validate_policy() -> None:
    """Refuse to start on a malformed list. Fails closed on a typo."""
    for word in FORBIDDEN_KEYWORDS | FORBIDDEN_LOCKING_WORDS | ALLOWED_STATEMENT_TYPES:
        if word != word.upper() or not word.strip():
            raise ValueError(f"sql_allowlist: {word!r} must be upper case and non-empty")
    for name in FORBIDDEN_NAMES | FORBIDDEN_FUNCTIONS:
        if name != name.lower() or not name.strip():
            raise ValueError(f"sql_allowlist: {name!r} must be lower case and non-empty")


validate_policy()
