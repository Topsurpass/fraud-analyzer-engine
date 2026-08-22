"""SELECT-only SQL validator.

Every statement, saved or ad-hoc, passes through :func:`validate_select` before
any target database sees it. Nothing else in the service is allowed to build a
statement and execute it directly.

Order of checks is load-bearing:

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

Why the keyword blocklist is deliberately short: ``sqlparse`` tags many
non-reserved words as keywords even when they are ordinary column names.
``SELECT comment, load, copy, call FROM audit_notes`` is valid SQL, and a
blanket keyword scan would reject it. So the blocklist holds only words that
are ANSI-reserved, meaning they cannot appear unquoted as an identifier, and a
quoted identifier tokenises as a name rather than a keyword. Everything else
dangerous is a command word, which check 3 already catches by position.
"""

from __future__ import annotations

import sqlparse
from sqlparse import tokens as T
from sqlparse.sql import Statement

from app.errors import ErrorCode, SqlValidationError

#: Token types that count as a keyword for the blocklist scan.
_KEYWORD_TYPES = (T.Keyword, T.Keyword.DML, T.Keyword.DDL, T.Keyword.DCL, T.Keyword.CTE)

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

#: Functions that read the filesystem, open a network connection, or burn CPU.
#: A statement can be a flawless SELECT and still exfiltrate through one of
#: these, so statement type alone is not enough.
FORBIDDEN_FUNCTIONS: frozenset[str] = frozenset(
    {
        # PostgreSQL
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "pg_sleep",
        "pg_sleep_for",
        "pg_sleep_until",
        "lo_import",
        "lo_export",
        "dblink",
        "dblink_exec",
        "dblink_connect",
        "query_to_xml",
        "query_to_xml_and_xmlschema",
        # MySQL
        "load_file",
        "benchmark",
        "sleep",
        # SQLite
        "readfile",
        "writefile",
        "load_extension",
        "edit",
    }
)


def _reject(code: ErrorCode, message: str, **detail: object) -> None:
    raise SqlValidationError(code, message, detail or None)


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
    """
    tokens = [t for t in statement.flatten() if not t.is_whitespace]
    for index, token in enumerate(tokens):
        if token.ttype not in (T.Name, T.Name.Builtin, T.Name.Placeholder) and (
            token.ttype not in _KEYWORD_TYPES
        ):
            continue
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if following is None or following.value != "(":
            continue
        name = token.value.strip('"`[]').lower()
        if name in FORBIDDEN_FUNCTIONS:
            _reject(
                ErrorCode.FORBIDDEN_FUNCTION,
                f"Forbidden function {name!r}: it can read the filesystem, "
                f"open a network connection, or stall the server.",
                function=name,
            )


def validate_select(sql: str) -> str:
    """Validate ``sql`` and return the sanitised text that is safe to execute.

    The return value is the comment-stripped statement without its trailing
    semicolon. Callers must execute exactly what is returned, never the
    original input, or the comment-stripping guarantee is lost.

    Raises:
        SqlValidationError: with a specific ``ErrorCode`` naming the reason.
    """
    if not isinstance(sql, str) or not sql.strip():
        _reject(ErrorCode.EMPTY_STATEMENT, "No SQL statement was provided.")

    stripped = sqlparse.format(sql, strip_comments=True).strip()
    if not stripped or not stripped.strip(";").strip():
        _reject(
            ErrorCode.EMPTY_STATEMENT,
            "The statement is empty once comments are removed.",
        )

    statements = _significant_statements(stripped)
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
        # splitting logic fails closed rather than indexing into an empty list.
        _reject(ErrorCode.EMPTY_STATEMENT, "No executable statement was found.")

    single = statements[0]
    parsed = sqlparse.parse(single)
    if not parsed:  # pragma: no cover - defensive
        # sqlparse returns a statement for any non-empty input, so this is
        # unreachable. Kept so a parser change fails closed.
        _reject(ErrorCode.INVALID_SQL, "The statement could not be parsed.")

    statement = parsed[0]
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

    return single
