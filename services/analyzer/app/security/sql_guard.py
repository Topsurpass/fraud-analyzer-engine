"""SELECT-only SQL validator.

Every statement, saved or ad-hoc, passes through :func:`validate_select` before
any target database sees it. Nothing else in the service is allowed to build a
statement and execute it directly.

Order of checks is load-bearing:

0. **Refuse input whose lexing is ambiguous.** Everything below reasons about
   tokens, so it is all worthless if ``sqlparse`` and the target engine
   disagree about where a string literal ends. See
   :func:`_check_unambiguous_lexing`; this check exists because they did, and
   the resulting bypass defeated every other check at once.
1. **Strip comments first, and execute the stripped text.** This is what
   defeats ``SELECT 1 -- \\n; DROP TABLE users``: splitting the raw text would
   see one statement because the ``;`` looks commented out, while the database
   would see two. Stripping before splitting removes the disguise. It also
   destroys MySQL's executable ``/*! ... */`` comments, whose body MySQL runs
   but every other parser treats as inert.
2. **Split and require exactly one statement.**
3. **Require the statement type to be SELECT.** ``sqlparse`` resolves a CTE to
   the command that follows it, so ``WITH x AS (...) DELETE FROM t`` reports
   ``DELETE`` and is rejected here. This single check also rejects ``PRAGMA``,
   ``ATTACH``, ``COPY``, ``LOAD DATA``, ``SET``, ``USE``, ``CALL``, ``EXPLAIN``,
   ``TABLE`` and ``VALUES``, none of which report as ``SELECT``.
4. **Keyword blocklist**, for writes hidden inside something that does report as
   a SELECT, such as ``WITH x AS (INSERT ... RETURNING *) SELECT * FROM x``.
5. **Function blocklist**, because a pure SELECT can still read the filesystem
   or open a network connection.

6. **Locking-clause check**, because ``FOR SHARE`` is a locking read that the
   keyword blocklist cannot catch without also rejecting a column named
   ``share``.

Why the keyword blocklist is deliberately short: ``sqlparse`` tags many
non-reserved words as keywords even when they are ordinary column names.
``SELECT comment, load, copy, call FROM audit_notes`` is valid SQL, and a
blanket keyword scan would reject it. So the blocklist holds only words that
are ANSI-reserved, meaning they cannot appear unquoted as an identifier, and a
quoted identifier tokenises as a name rather than a keyword. Everything else
dangerous is a command word, which check 3 already catches by position.

**Why the function check must accept quoted identifiers.** ``sqlparse`` types a
double-quoted identifier as ``Token.Literal.String.Symbol``, not ``Token.Name``.
An earlier version of this module scanned only name and keyword tokens, so
``SELECT "pg_read_file"('/etc/passwd')`` walked straight past the function
blocklist while the bare form was rejected -- PostgreSQL accepts both. Two
quote characters defeated the entire list. ``_NAME_TYPES`` exists to keep that
fixed, and the corpus in ``tests/test_sql_guard.py`` pins every blocklisted
function in bare, double-quoted, backticked and bracketed form.

Note that quoting is deliberately *not* honoured in :func:`_check_keywords`'s
``FORBIDDEN_NAMES`` branch. That branch matches a bare word anywhere in the
statement, so accepting quoted forms there would reject ``SELECT "pragma" FROM
notes``, a legal query against a legal column, for no gain: all three entries
are reachable only through ``INTO`` or a statement type that check 3 already
rejects.

**What the comment-stripping guarantee does and does not cover.** It defeats
stacking that hides the ``;`` behind a comment, and it destroys MySQL's
executable ``/*! ... */`` comments. It does *not* by itself defeat stacking in
general: an attacker who can make ``sqlparse`` disagree with the engine about
string boundaries can stack statements the splitter never sees. That is check
0's job, and until check 0 existed the guarantee in check 1 was overstated.

**On ``set_config`` and the read-only layers.** Blocking ``set_config`` stops
one spelling of "turn the read-only transaction default off". It is not the
thing that makes disabling it fail. SQLAlchemy runs each statement inside a
transaction and rolls back when the connection returns to the pool, and
PostgreSQL reverts a session ``SET`` made inside a rolled-back transaction, so
the change does not survive into the next request that reuses the handle. The
blocklist entry is defence in depth; the rollback is the actual protection.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict

import sqlparse
from sqlparse import tokens as T
from sqlparse.exceptions import SQLParseError
from sqlparse.sql import Statement

from app.config import get_settings
from app.errors import ErrorCode, SqlValidationError

#: Token types that count as a keyword for the blocklist scan.
_KEYWORD_TYPES = (T.Keyword, T.Keyword.DML, T.Keyword.DDL, T.Keyword.DCL, T.Keyword.CTE)

#: Token types that can carry a function's name in a call expression.
#: ``String.Symbol`` is what a double-quoted identifier becomes, and leaving it
#: out is what made every blocklisted function reachable by quoting it.
_NAME_TYPES = (T.Name, T.Name.Builtin, T.Name.Placeholder, T.String.Symbol)

#: Quote characters stripped from an identifier before it is matched.
_QUOTES = '"`[]'

#: Constructs where ``sqlparse`` and the target engines disagree about where a
#: string literal or identifier ends. See :func:`_check_unambiguous_lexing`.
#:
#: ``\'`` is the dangerous one. sqlparse's string pattern is
#: ``'(''|\\'|[^'])*'``, so a backslash-escaped quote keeps the string *open*.
#: PostgreSQL with ``standard_conforming_strings = on`` -- the default on every
#: supported version since 9.1 -- treats a backslash as an ordinary character,
#: so the string *closes* at that quote. SQLite behaves like PostgreSQL. The
#: two parsers then disagree about which characters are inert text, and
#: everything this module checks is computed against the wrong tokens.
_BACKSLASH_QUOTE = "\\'"

#: PostgreSQL's unicode-escaped identifier and string syntax. sqlparse does not
#: decode the escapes, so ``U&"pg_re\0061d_file"`` reaches the function
#: blocklist spelled differently from the function it actually calls.
_UNICODE_ESCAPE_LITERAL = re.compile(r"(?<![A-Za-z0-9_])[uU]&\s*['\"]")

#: Memoised acceptances, keyed on the exact input string. See validate_select.
_validated: OrderedDict[str, str] = OrderedDict()
_cache_lock = threading.RLock()

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
_POSTGRES_FUNCTIONS: frozenset[str] = frozenset(
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
_MYSQL_FUNCTIONS: frozenset[str] = frozenset(
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
_SQLITE_FUNCTIONS: frozenset[str] = frozenset(
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
    _POSTGRES_FUNCTIONS | _MYSQL_FUNCTIONS | _SQLITE_FUNCTIONS
)


def _reject(code: ErrorCode, message: str, **detail: object) -> None:
    raise SqlValidationError(code, message, detail or None)


def _check_unambiguous_lexing(sql: str) -> None:
    """Reject input whose string boundaries this module cannot trust.

    This runs before anything is parsed, because it is a check on whether
    parsing means anything at all.

    Every other check in this module reasons about tokens: which words are
    keywords, which names are followed by ``(``, how many statements there
    are. All of that is computed from ``sqlparse``'s view of where string
    literals start and end. Where the target engine disagrees with that view,
    the guard inspects one statement and the database executes a different
    one -- and the caller executes the string this function's caller returns,
    so the difference is live SQL.

    The concrete break this exists to close::

        SELECT 'x\\', pg_read_file('/etc/hostname') AS leak, 'y'

    sqlparse reads ``'x\\', pg_read_file('`` as a single string literal, so it
    sees no function call, one statement, and no forbidden token: it passes
    every check. PostgreSQL closes the string at ``'x\\'`` and runs
    ``pg_read_file`` for real, returning the file contents in the result set
    the API hands back. Verified end to end against PostgreSQL 16; the same
    trick stacks statements past the "exactly one statement" check
    (``SELECT 'a\\'; SELECT pg_sleep(2); --'`` slept for two seconds).

    Rejecting is the right response rather than trying to lex it correctly.
    There is no single correct answer -- MySQL really does treat ``\\'`` as an
    escape while PostgreSQL and SQLite do not -- so any statement containing
    the sequence is ambiguous *by definition* across the engines this service
    supports. The portable spelling of a literal quote is ``''``, which is the
    SQL standard and is unaffected.
    """
    if _BACKSLASH_QUOTE in sql:
        _reject(
            ErrorCode.INVALID_SQL,
            "The statement contains a backslash before a quote (\\'), which "
            "means different things on different databases and cannot be "
            "validated safely. Write a literal quote as '' instead.",
        )

    if _UNICODE_ESCAPE_LITERAL.search(sql):
        _reject(
            ErrorCode.INVALID_SQL,
            "Unicode-escaped literals (U&'...' / U&\"...\") are not accepted: "
            "the escapes hide the real identifier from validation.",
        )


def _parsed(func, *args, **kwargs):
    """Run a sqlparse call, turning a parser failure into ``INVALID_SQL``.

    ``sqlparse`` raises ``SQLParseError`` on input it cannot tokenise, most
    reachably by exceeding its 10,000-token ceiling. Uncaught it became a 500
    with no useful body, while the contract promises ``INVALID_SQL`` / 400 for
    a statement that could not be parsed. Failing closed here is also what
    keeps a parser change from turning into an accidental bypass.
    """
    try:
        return func(*args, **kwargs)
    except SQLParseError as exc:
        _reject(ErrorCode.INVALID_SQL, f"The statement could not be parsed: {exc}")
    except RecursionError:
        # Deeply nested parentheses blow the parser's own stack before the
        # token ceiling is reached.
        _reject(ErrorCode.INVALID_SQL, "The statement is nested too deeply to parse.")


def _significant_statements(sql: str) -> list[str]:
    """Split into statements, discarding empties and bare semicolons."""
    parts = []
    for part in sqlparse.split(sql):
        cleaned = part.strip().rstrip(";").strip()
        if cleaned:
            parts.append(cleaned)
    return parts


def _check_keywords(statement: Statement) -> None:
    for token in statement.flatten():
        if token.ttype in _KEYWORD_TYPES:
            word = token.value.upper()
            if word in FORBIDDEN_KEYWORDS:
                _reject(
                    ErrorCode.FORBIDDEN_KEYWORD,
                    f"Forbidden keyword {word!r}: only read-only SELECT "
                    f"statements are permitted.",
                    keyword=word,
                )
        elif token.ttype in (T.Name, T.Name.Builtin):
            if token.value.lower() in FORBIDDEN_NAMES:
                _reject(
                    ErrorCode.FORBIDDEN_KEYWORD,
                    f"Forbidden keyword {token.value.upper()!r}: only "
                    f"read-only SELECT statements are permitted.",
                    keyword=token.value.upper(),
                )


def _check_functions(statement: Statement) -> None:
    """Reject a call to any blocklisted function.

    A function call is a name or keyword token followed, ignoring whitespace,
    by an opening parenthesis. Matching on the call shape rather than the bare
    word means a column happening to be named ``sleep`` still works.

    Quoted identifiers count. ``"pg_read_file"(...)``, ``` `load_file`(...) ```
    and ``[readfile](...)`` are all calls to the same function as far as the
    server is concerned, and each quoting style lands on a different sqlparse
    token type, so all of them are matched here after the quotes are stripped.

    A call does not need a parenthesis at all. PostgreSQL's functional
    notation makes ``(x).f`` and ``x.f`` mean ``f(x)``, so a name preceded by
    a dot is also treated as a call. The cost is that a *column* named after a
    blocklisted function can no longer be read through a qualified reference:
    ``SELECT t.sleep FROM t`` is rejected where ``SELECT sleep FROM t`` still
    works. That is a deliberate trade -- the unqualified form is not a call on
    any supported engine, and the qualified form reads arbitrary files on
    PostgreSQL.
    """
    tokens = [t for t in statement.flatten() if not t.is_whitespace]
    for index, token in enumerate(tokens):
        if token.ttype not in _NAME_TYPES and token.ttype not in _KEYWORD_TYPES:
            continue

        name = token.value.strip(_QUOTES).lower()
        if name not in FORBIDDEN_FUNCTIONS:
            continue

        following = tokens[index + 1] if index + 1 < len(tokens) else None
        preceding = tokens[index - 1] if index else None

        called_with_parens = following is not None and following.value == "("
        # PostgreSQL's functional notation: `(x).f` and `x.f` both mean `f(x)`.
        # This reaches a function with no parenthesis after its name at all,
        # which is the shape the check above keys on. Confirmed against
        # PostgreSQL 16: `SELECT ('/etc/hostname'::text).pg_read_file`
        # returned the file's contents, and `SELECT (2.0::float8).pg_sleep`
        # slept for two seconds.
        # Only PostgreSQL has this syntax, so only PostgreSQL's functions are
        # reachable through it. Applying the rule to the whole blocklist would
        # reject `SELECT a.edit FROM a` -- an ordinary column reference, since
        # `edit` is only a function on SQLite, which has no field notation.
        called_as_field = (
            preceding is not None
            and preceding.value == "."
            and name in _POSTGRES_FUNCTIONS
        )

        if not (called_with_parens or called_as_field):
            continue

        _reject(
            ErrorCode.FORBIDDEN_FUNCTION,
            f"Forbidden function {name!r}: it can read the filesystem, "
            f"open a network connection, change session settings, or "
            f"stall the server.",
            function=name,
        )


def _check_locking_clause(statement: Statement) -> None:
    """Reject ``FOR SHARE`` and ``FOR KEY SHARE``.

    A locking read has no place in an analytics query: it takes row locks on
    the customer's production tables and holds them for the length of the
    transaction. ``FOR UPDATE`` and ``FOR NO KEY UPDATE`` are already rejected
    because ``UPDATE`` is on the keyword blocklist, but ``SHARE`` cannot go on
    that list -- ``sqlparse`` types a bare ``share`` as a keyword too, so
    blocking the word would reject ``SELECT share FROM positions``.

    Anchoring on the ``FOR ... SHARE`` sequence is what separates the locking
    clause from the column name. ``NO`` and ``KEY`` are skipped so the
    PostgreSQL long forms are covered.
    """
    tokens = [t for t in statement.flatten() if not t.is_whitespace]
    for index, token in enumerate(tokens):
        if token.ttype not in _KEYWORD_TYPES or token.value.upper() != "FOR":
            continue
        for following in tokens[index + 1 :]:
            if following.ttype not in _KEYWORD_TYPES:
                break
            word = following.value.upper()
            if word in ("NO", "KEY"):
                continue
            if word == "SHARE":
                _reject(
                    ErrorCode.FORBIDDEN_KEYWORD,
                    "Forbidden locking clause 'FOR SHARE': it takes row locks "
                    "on the target database. Only plain reads are permitted.",
                    keyword="FOR SHARE",
                )
            break


def validate_select(sql: str) -> str:
    """Validate ``sql`` and return the sanitised text that is safe to execute.

    The return value is the comment-stripped statement without its trailing
    semicolon. Callers must execute exactly what is returned, never the
    original input, or the comment-stripping guarantee is lost.

    Accepted results are memoised on the exact input string. A saved query
    sends byte-identical SQL on every run and on every cache-missing poll, and
    parsing is the most expensive thing in the request that does not touch a
    database, so revalidating it each time was pure repeated cost. Only
    acceptances are cached: a rejection re-runs, which keeps the rejection path
    honest and means a flood of distinct hostile statements cannot be made
    cheap by the cache.

    The cache is keyed on the raw string, so it can never return a verdict for
    different text, and it is cleared whenever settings change.

    Raises:
        SqlValidationError: with a specific ``ErrorCode`` naming the reason.
    """
    if not isinstance(sql, str):
        _reject(ErrorCode.EMPTY_STATEMENT, "No SQL statement was provided.")

    cached = _validated.get(sql)
    if cached is not None:
        return cached

    result = _validate_uncached(sql)

    limit = get_settings().sql_validation_cache_size
    if limit > 0:
        with _cache_lock:
            _validated[sql] = result
            _validated.move_to_end(sql)
            while len(_validated) > limit:
                _validated.popitem(last=False)
    return result


def clear_validation_cache() -> None:
    """Drop every memoised verdict. Called whenever settings change."""
    with _cache_lock:
        _validated.clear()


def _validate_uncached(sql: str) -> str:
    if not isinstance(sql, str) or not sql.strip():
        _reject(ErrorCode.EMPTY_STATEMENT, "No SQL statement was provided.")

    # Length is checked before anything parses. sqlparse is pure Python and
    # superlinear on pathological input, so an unbounded statement is a CPU
    # denial of service that never reaches a database and is therefore never
    # bounded by the statement timeout.
    max_length = get_settings().max_sql_length
    if len(sql) > max_length:
        _reject(
            ErrorCode.SQL_TOO_LONG,
            f"The statement is {len(sql)} characters, over the "
            f"{max_length} character limit.",
            length=len(sql),
            max_length=max_length,
        )

    # Before parsing, because this decides whether parsing means anything.
    _check_unambiguous_lexing(sql)

    stripped = _parsed(sqlparse.format, sql, strip_comments=True).strip()
    if not stripped or not stripped.strip(";").strip():
        _reject(
            ErrorCode.EMPTY_STATEMENT,
            "The statement is empty once comments are removed.",
        )

    # One parse, not two. sqlparse.split() and sqlparse.parse() each do a full
    # parse of the same text, and parse() already returns one Statement per
    # semicolon-separated statement, so splitting separately was paying for the
    # identical work twice. Comment stripping still happens first -- that
    # ordering is the thing that defeats "SELECT 1 -- \n; DROP TABLE users" and
    # is not negotiable -- but everything after it now runs once.
    statements = [
        statement
        for statement in _parsed(sqlparse.parse, stripped)
        if str(statement).strip().rstrip(";").strip()
    ]

    if len(statements) > 1:
        _reject(
            ErrorCode.MULTIPLE_STATEMENTS,
            f"Multiple statements are not allowed; found {len(statements)}. "
            f"Submit exactly one SELECT.",
            statement_count=len(statements),
        )
    if not statements:  # pragma: no cover - defensive
        # Unreachable today: the emptiness check above already guarantees at
        # least one significant statement. Kept so a future change to the
        # parsing logic fails closed rather than indexing into an empty list.
        _reject(ErrorCode.EMPTY_STATEMENT, "No executable statement was found.")

    statement = statements[0]
    single = str(statement).strip().rstrip(";").strip()
    statement_type = statement.get_type()
    if statement_type != "SELECT":
        _reject(
            ErrorCode.NON_SELECT_STATEMENT,
            f"Only SELECT statements are permitted; this is "
            f"{statement_type if statement_type != 'UNKNOWN' else 'not a SELECT'}.",
            statement_type=statement_type,
        )

    _check_keywords(statement)
    _check_functions(statement)
    _check_locking_clause(statement)

    # The caller executes this exact string, so it gets the lexing check too.
    # Comment stripping rewrites the text, and a rewrite that reintroduced an
    # ambiguous sequence would otherwise ship unvalidated.
    _check_unambiguous_lexing(single)

    return single
