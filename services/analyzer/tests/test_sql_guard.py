"""Adversarial corpus for the SELECT-only guard.

This file is the quality gate for the highest-risk component in the service.
A single bypass here means the fraud tool is itself an attack surface.
"""

import time

import pytest
from sqlparse.exceptions import SQLParseError

from app.config import get_settings
from app.errors import ErrorCode, SqlValidationError
from app.security import sql_guard
from app.security.sql_guard import validate_select

# --------------------------------------------------------------------------
# Statements that MUST be accepted. False positives here break real analytics.
# --------------------------------------------------------------------------
ACCEPTED = [
    "SELECT 1",
    "select * from txns",
    "SELECT day, count(*) FROM txns GROUP BY day ORDER BY day",
    "WITH f AS (SELECT * FROM txns WHERE flagged) SELECT count(*) FROM f",
    "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r WHERE n<5)"
    " SELECT n FROM r",
    "SELECT a FROM t UNION ALL SELECT b FROM u",
    "SELECT sum(amount) OVER (PARTITION BY user_id) FROM txns",
    "SELECT * FROM a JOIN b ON a.id = b.a_id LEFT JOIN c ON c.b_id = b.id",
    "SELECT CASE WHEN amount > 100 THEN 'high' ELSE 'low' END AS band FROM t",
    "SELECT * FROM t LIMIT 10;",
    "SELECT * FROM t OFFSET 5 ROWS FETCH NEXT 10 ROWS ONLY",
    # Non-reserved words that sqlparse tags as keywords but are legal columns.
    "SELECT comment, load, copy, call, rename FROM audit_notes",
    "SELECT REPLACE(name, 'a', 'b') FROM t",
    # Reserved words as forbidden text, but only inside a string literal.
    "SELECT 'DROP TABLE users' AS note",
    "SELECT * FROM t WHERE note = 'delete from accounts'",
    # Keyword appearing only as a substring of an identifier.
    "SELECT created_at, updated_at, deleted_at FROM t",
    "SELECT insertion_id, creator FROM t",
    # Comments are allowed; they are stripped before execution.
    "SELECT 1 /* a note */ FROM t",
    "-- leading note\nSELECT 1 FROM t",
    # The locking-clause check anchors on FOR, so a column called 'share' is
    # still a column. sqlparse types bare 'share', 'key' and 'no' as keywords,
    # which is exactly why they cannot go on the keyword blocklist.
    "SELECT share FROM positions",
    "SELECT key, no, share FROM t",
    "SELECT share_count FROM t WHERE share > 0",
    # A quoted identifier is only treated as a function name when it is
    # actually called. As a column it stays legal.
    'SELECT "pragma" FROM notes',
    'SELECT "sleep" FROM naps',
    # Bounded by the statement timeout and the byte budget, not a blocklist:
    # both are ordinary in analytics and blocking them would hurt real work.
    "SELECT generate_series(1, 10)",
    "SELECT repeat(name, 2) FROM t",
]

# --------------------------------------------------------------------------
# Statements that MUST be rejected, each with its exact error code.
# --------------------------------------------------------------------------
REJECTED = [
    # --- statement stacking -------------------------------------------------
    ("SELECT 1; DROP TABLE users", ErrorCode.MULTIPLE_STATEMENTS),
    ("SELECT 1 -- \n; DROP TABLE users", ErrorCode.MULTIPLE_STATEMENTS),
    ("SELECT 1 /* x */; DELETE FROM t", ErrorCode.MULTIPLE_STATEMENTS),
    ("SELECT 1;\nUPDATE t SET a=1", ErrorCode.MULTIPLE_STATEMENTS),
    ("SELECT 1;;DROP TABLE t", ErrorCode.MULTIPLE_STATEMENTS),
    # --- non-SELECT commands ------------------------------------------------
    ("DROP TABLE users", ErrorCode.NON_SELECT_STATEMENT),
    ("DELETE FROM txns", ErrorCode.NON_SELECT_STATEMENT),
    ("UPDATE t SET a = 1", ErrorCode.NON_SELECT_STATEMENT),
    ("INSERT INTO t VALUES (1)", ErrorCode.NON_SELECT_STATEMENT),
    ("CREATE TABLE t (a int)", ErrorCode.NON_SELECT_STATEMENT),
    ("ALTER TABLE t ADD COLUMN b int", ErrorCode.NON_SELECT_STATEMENT),
    ("TRUNCATE TABLE t", ErrorCode.NON_SELECT_STATEMENT),
    ("GRANT ALL ON t TO public", ErrorCode.NON_SELECT_STATEMENT),
    ("REVOKE ALL ON t FROM public", ErrorCode.NON_SELECT_STATEMENT),
    ("ATTACH DATABASE '/tmp/x.db' AS x", ErrorCode.NON_SELECT_STATEMENT),
    ("PRAGMA table_info(t)", ErrorCode.NON_SELECT_STATEMENT),
    ("COPY t TO PROGRAM 'sh -c whoami'", ErrorCode.NON_SELECT_STATEMENT),
    ("LOAD DATA INFILE '/etc/passwd' INTO TABLE t", ErrorCode.NON_SELECT_STATEMENT),
    ("SET default_transaction_read_only = off", ErrorCode.NON_SELECT_STATEMENT),
    ("USE otherdb", ErrorCode.NON_SELECT_STATEMENT),
    ("CALL sp_evil()", ErrorCode.NON_SELECT_STATEMENT),
    ("EXPLAIN SELECT 1", ErrorCode.NON_SELECT_STATEMENT),
    ("VACUUM", ErrorCode.NON_SELECT_STATEMENT),
    ("BEGIN; SELECT 1", ErrorCode.MULTIPLE_STATEMENTS),
    ("TABLE users", ErrorCode.NON_SELECT_STATEMENT),
    ("VALUES (1),(2)", ErrorCode.NON_SELECT_STATEMENT),
    # --- CTE-wrapped writes: statement type says SELECT, body does not ------
    (
        "WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x",
        ErrorCode.FORBIDDEN_KEYWORD,
    ),
    (
        "WITH x AS (UPDATE t SET a=1 RETURNING *) SELECT * FROM x",
        ErrorCode.FORBIDDEN_KEYWORD,
    ),
    (
        "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
        ErrorCode.FORBIDDEN_KEYWORD,
    ),
    ("WITH x AS (SELECT 1) DELETE FROM t", ErrorCode.NON_SELECT_STATEMENT),
    ("WITH x AS (SELECT 1) UPDATE t SET a=1", ErrorCode.NON_SELECT_STATEMENT),
    # --- write / exfil clauses inside a genuine SELECT ----------------------
    ("SELECT * INTO newtbl FROM t", ErrorCode.FORBIDDEN_KEYWORD),
    ("SELECT * FROM t INTO OUTFILE '/tmp/x'", ErrorCode.FORBIDDEN_KEYWORD),
    ("SELECT * FROM t INTO DUMPFILE '/tmp/x'", ErrorCode.FORBIDDEN_KEYWORD),
    ("SELECT * FROM t FOR UPDATE", ErrorCode.FORBIDDEN_KEYWORD),
    ("SELECT a FROM t UNION SELECT 1 INTO OUTFILE '/tmp/x'", ErrorCode.FORBIDDEN_KEYWORD),
    ("select * from t where id in (select 1) ; drop table t", ErrorCode.MULTIPLE_STATEMENTS),
    # --- file / network exfil functions -------------------------------------
    ("SELECT pg_read_file('/etc/passwd')", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT pg_read_binary_file('/etc/shadow')", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT pg_ls_dir('/')", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT lo_import('/etc/passwd')", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT lo_export(1, '/tmp/out')", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT dblink('host=evil', 'SELECT 1')", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT load_file('/etc/passwd')", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT readfile('/etc/passwd')", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT writefile('/tmp/x', 'y')", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT load_extension('evil.so')", ErrorCode.FORBIDDEN_FUNCTION),
    # casing and whitespace must not evade the check
    ("SELECT PG_READ_FILE('/etc/passwd')", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT Load_File('/etc/passwd')", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT pg_read_file  ('/etc/passwd')", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT pg_read_file\n('/etc/passwd')", ErrorCode.FORBIDDEN_FUNCTION),
    # --- quoted function names ----------------------------------------------
    # Regression: sqlparse types a double-quoted identifier as String.Symbol,
    # not Name. The guard skipped that type, so two quote characters walked
    # every entry in FORBIDDEN_FUNCTIONS straight past the blocklist while the
    # bare form was correctly rejected. PostgreSQL accepts both spellings.
    ("SELECT \"pg_read_file\"('/etc/passwd')", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT \"PG_READ_FILE\"('/etc/passwd')", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT \"pg_sleep\"(300)", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT \"readfile\"('/etc/passwd')", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT \"lo_import\"('/etc/passwd')", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT pg_catalog.\"pg_read_file\"(1)", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT `load_file`('/etc/passwd')", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT [readfile]('/etc/passwd')", ErrorCode.FORBIDDEN_FUNCTION),
    # --- session-state escalation --------------------------------------------
    # set_config disables default_transaction_read_only for the life of the
    # pooled connection, which is the middle layer of the read-only guarantee.
    (
        "SELECT set_config('default_transaction_read_only', 'off', false)",
        ErrorCode.FORBIDDEN_FUNCTION,
    ),
    (
        "SELECT \"set_config\"('default_transaction_read_only', 'off', false)",
        ErrorCode.FORBIDDEN_FUNCTION,
    ),
    # --- writes through a function -------------------------------------------
    ("SELECT nextval('seq')", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT setval('seq', 1)", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT lo_put(1, 0, 'x')", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT lo_unlink(1)", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT pg_notify('chan', 'payload')", ErrorCode.FORBIDDEN_FUNCTION),
    # --- server control ------------------------------------------------------
    ("SELECT pg_terminate_backend(1)", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT pg_cancel_backend(1)", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT pg_reload_conf()", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT pg_promote()", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT pg_stat_reset()", ErrorCode.FORBIDDEN_FUNCTION),
    # --- sibling exfil functions the original list missed --------------------
    ("SELECT lo_get(1)", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT dblink_send_query('c', 'SELECT 1')", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT dblink_fetch('c', 1)", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT pg_ls_waldir()", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT query_to_xmlschema('SELECT 1', false, false, '')", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT fts3_tokenizer('x')", ErrorCode.FORBIDDEN_FUNCTION),
    # --- locking reads -------------------------------------------------------
    ("SELECT * FROM t FOR SHARE", ErrorCode.FORBIDDEN_KEYWORD),
    ("SELECT * FROM t FOR KEY SHARE", ErrorCode.FORBIDDEN_KEYWORD),
    ("SELECT * FROM t FOR NO KEY UPDATE", ErrorCode.FORBIDDEN_KEYWORD),
    ("SELECT * FROM t FOR SHARE OF t SKIP LOCKED", ErrorCode.FORBIDDEN_KEYWORD),
    # --- denial of service ---------------------------------------------------
    ("SELECT pg_sleep(30)", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT sleep(30)", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT benchmark(100000000, md5('a'))", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT get_lock('a', 10)", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT pg_advisory_lock(1)", ErrorCode.FORBIDDEN_FUNCTION),
    # --- nothing to run ------------------------------------------------------
    ("", ErrorCode.EMPTY_STATEMENT),
    ("   \n\t ", ErrorCode.EMPTY_STATEMENT),
    ("-- just a comment", ErrorCode.EMPTY_STATEMENT),
    ("/* only a block comment */", ErrorCode.EMPTY_STATEMENT),
    (";", ErrorCode.EMPTY_STATEMENT),
]


@pytest.mark.parametrize("sql", ACCEPTED)
def test_accepted(sql):
    assert validate_select(sql).strip()


@pytest.mark.parametrize("sql,code", REJECTED, ids=[s[:48] for s, _ in REJECTED])
def test_rejected(sql, code):
    with pytest.raises(SqlValidationError) as ei:
        validate_select(sql)
    assert ei.value.error_code == code, f"{sql!r} -> {ei.value.error_code}"


def test_leading_semicolon_is_stripped_not_executed():
    # A stray leading ";" yields one significant statement. The guard returns
    # the sanitised text, so the semicolon never reaches the driver.
    assert validate_select("  ; SELECT 1") == "SELECT 1"


def test_leading_semicolon_does_not_smuggle_a_write():
    with pytest.raises(SqlValidationError) as ei:
        validate_select("; DROP TABLE t")
    assert ei.value.error_code == ErrorCode.NON_SELECT_STATEMENT


def test_rejection_names_the_offending_token():
    with pytest.raises(SqlValidationError) as ei:
        validate_select("SELECT pg_read_file('/etc/passwd')")
    assert "pg_read_file" in ei.value.message.lower()


def test_comments_are_stripped_from_returned_sql():
    out = validate_select("SELECT 1 /* hi */ FROM t -- tail")
    assert "hi" not in out
    assert "tail" not in out


def test_mysql_executable_comment_payload_is_destroyed():
    # MySQL runs the body of /*! ... */. Stripping removes it before execution.
    out = validate_select("SELECT 1 /*!32302 UNION SELECT 2 */")
    assert "UNION" not in out.upper()


def test_trailing_semicolon_removed():
    assert not validate_select("SELECT 1;").rstrip().endswith(";")


def test_returned_sql_is_what_gets_executed():
    # The guard must return the sanitised text, never the caller's original.
    original = "SELECT 1 -- ; DROP TABLE t"
    assert "DROP" not in validate_select(original).upper()


def test_non_string_input_rejected():
    with pytest.raises(SqlValidationError) as ei:
        validate_select(None)  # type: ignore[arg-type]
    assert ei.value.error_code == ErrorCode.EMPTY_STATEMENT


def test_error_carries_http_400():
    with pytest.raises(SqlValidationError) as ei:
        validate_select("DROP TABLE t")
    assert ei.value.http_status == 400


def test_name_typed_exfil_words_are_rejected():
    # sqlparse types OUTFILE and DUMPFILE as plain names, not keywords, so they
    # need their own check when they appear without a preceding INTO.
    for sql in ("SELECT outfile FROM t", "SELECT dumpfile FROM t"):
        with pytest.raises(SqlValidationError) as ei:
            validate_select(sql)
        assert ei.value.error_code == ErrorCode.FORBIDDEN_KEYWORD


def test_forbidden_name_rejection_names_the_token():
    with pytest.raises(SqlValidationError) as ei:
        validate_select("SELECT outfile FROM t")
    assert "OUTFILE" in ei.value.message


# --------------------------------------------------------------------------
# Length cap and parser failure
#
# Regression for two defects in one request: a 1.6 MB statement measured at
# 51 seconds of pure-Python CPU and then returned a 500, because sqlparse's
# SQLParseError was uncaught. The statement never reaches a database, so the
# server-side statement timeout does not bound it, and enough concurrent
# requests of that shape exhaust the request threadpool.
# --------------------------------------------------------------------------


def test_oversized_statement_is_rejected_before_parsing():
    settings = get_settings()
    oversized = "SELECT " + ("a," * settings.max_sql_length) + "1"

    started = time.perf_counter()
    with pytest.raises(SqlValidationError) as ei:
        validate_select(oversized)
    elapsed = time.perf_counter() - started

    assert ei.value.error_code == ErrorCode.SQL_TOO_LONG
    assert ei.value.http_status == 400
    # The point of the cap is that rejection costs nothing. Parsing this same
    # input is what took 51 seconds.
    assert elapsed < 0.5, f"rejection took {elapsed:.2f}s; the cap is not short-circuiting"


def test_length_cap_reports_both_numbers():
    settings = get_settings()
    with pytest.raises(SqlValidationError) as ei:
        validate_select("SELECT " + "a" * (settings.max_sql_length + 10))
    assert ei.value.detail["max_length"] == settings.max_sql_length
    assert ei.value.detail["length"] > settings.max_sql_length


def test_statement_at_the_limit_is_still_parsed():
    """The cap rejects only what is over it, never what is exactly at it.

    The filler is one long string literal rather than repeated tokens:
    sqlparse caps a statement at 10,000 tokens independently of this setting,
    and the point here is the character cap, not that ceiling.
    """
    settings = get_settings()
    literal_len = settings.max_sql_length - len("SELECT ''")
    sql = "SELECT '" + ("a" * literal_len) + "'"
    assert len(sql) == settings.max_sql_length
    assert validate_select(sql) == sql


@pytest.mark.slow
def test_worst_case_accepted_statement_stays_under_a_cpu_budget():
    """The cap exists to bound CPU, so the bound is what gets asserted.

    A statement one character under the limit, made of the densest tokens
    sqlparse has to group, is the most expensive input the guard will ever
    accept for parsing. Unbounded, this same shape at 1.6 MB measured 51 s.
    """
    settings = get_settings()
    pairs = (settings.max_sql_length - len("SELECT 1")) // 2
    sql = "SELECT " + ("a," * pairs) + "1"
    assert len(sql) <= settings.max_sql_length

    started = time.perf_counter()
    validate_select(sql)
    elapsed = time.perf_counter() - started

    # The budget is the reason max_sql_length has the value it has. If a
    # sqlparse upgrade or a raised cap pushes this over, that is a real
    # regression in how much CPU one anonymous request can spend, not a flaky
    # timing test -- the margin is wide enough that ordinary jitter is silent.
    assert elapsed < 9.0, (
        f"worst-case accepted input took {elapsed:.2f}s; "
        f"lower max_sql_length (currently {settings.max_sql_length})"
    )


@pytest.mark.slow
def test_validation_is_memoised_for_repeated_identical_sql():
    """A saved query revalidates identical SQL on every run and every poll.

    Parsing is the most expensive thing in the request that never touches a
    database, so the second pass has to be free.
    """
    settings = get_settings()
    pairs = (settings.max_sql_length - len("SELECT 1")) // 2
    sql = "SELECT " + ("a," * pairs) + "1"

    started = time.perf_counter()
    first = validate_select(sql)
    cold = time.perf_counter() - started

    started = time.perf_counter()
    second = validate_select(sql)
    warm = time.perf_counter() - started

    assert first == second
    assert warm < cold / 10, f"cold {cold:.3f}s, warm {warm:.3f}s: not memoised"


def test_only_acceptances_are_memoised():
    """A rejection must re-run, so the reject path can never go stale."""
    sql_guard.clear_validation_cache()
    for _ in range(2):
        with pytest.raises(SqlValidationError):
            validate_select("SELECT pg_read_file('/etc/passwd')")
    assert "SELECT pg_read_file('/etc/passwd')" not in sql_guard._validated


def test_cache_cannot_return_a_verdict_for_different_text():
    """Keyed on the exact input, so two statements can never share a verdict."""
    a = validate_select("SELECT 1 FROM t")
    b = validate_select("SELECT 2 FROM t")
    assert a != b
    assert validate_select("SELECT 1 FROM t") == a


def test_unparseable_statement_maps_to_invalid_sql(monkeypatch):
    """INVALID_SQL is a contract code, so something must be able to emit it.

    Before this, every path to it was marked unreachable and a real
    SQLParseError escaped as a 500 with no useful body.
    """
    def boom(*_args, **_kwargs):
        raise SQLParseError("Maximum number of tokens exceeded (10000)")

    monkeypatch.setattr(sql_guard.sqlparse, "parse", boom)

    with pytest.raises(SqlValidationError) as ei:
        validate_select("SELECT 1")
    assert ei.value.error_code == ErrorCode.INVALID_SQL
    assert ei.value.http_status == 400


def test_deeply_nested_input_maps_to_invalid_sql(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(sql_guard.sqlparse, "parse", boom)

    with pytest.raises(SqlValidationError) as ei:
        validate_select("SELECT 1")
    assert ei.value.error_code == ErrorCode.INVALID_SQL


# --------------------------------------------------------------------------
# Quoting: the bypass that motivated _NAME_TYPES
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(sql_guard.FORBIDDEN_FUNCTIONS))
def test_every_blocklisted_function_is_blocked_in_all_quoting_styles(name):
    """A blocklist with a quoting hole in it is not a blocklist.

    Each style lands on a different sqlparse token type: bare and bracketed on
    Name, backticked on Name, double-quoted on String.Symbol. Missing the last
    one is what made the whole list bypassable.
    """
    for sql in (
        f"SELECT {name}(1)",
        f'SELECT "{name}"(1)',
        f"SELECT `{name}`(1)",
        f"SELECT [{name}](1)",
        f"SELECT {name.upper()}(1)",
        f'SELECT "{name.upper()}"(1)',
    ):
        with pytest.raises(SqlValidationError) as ei:
            validate_select(sql)
        assert ei.value.error_code == ErrorCode.FORBIDDEN_FUNCTION, sql


def test_quoted_name_is_only_forbidden_when_actually_called():
    """Quoting is honoured for calls, not for column references.

    Rejecting a quoted name everywhere would break `SELECT "pragma" FROM t`,
    a legal query against a legal column, for no security gain.
    """
    assert validate_select('SELECT "pg_read_file" FROM t')
    with pytest.raises(SqlValidationError):
        validate_select('SELECT "pg_read_file"(1)')


def test_set_config_cannot_disable_the_read_only_layer():
    """The specific escalation: turning off the connection's read-only default.

    postgres_connect_args sets default_transaction_read_only=on as a libpq
    option, and the pool reuses the handle, so a successful set_config would
    leave writes enabled for every later request on that connection.
    """
    with pytest.raises(SqlValidationError) as ei:
        validate_select(
            "SELECT set_config('default_transaction_read_only', 'off', false)"
        )
    assert ei.value.error_code == ErrorCode.FORBIDDEN_FUNCTION
    assert "set_config" in ei.value.message


def test_locking_clause_does_not_reject_a_column_named_share():
    """The FOR-sequence anchor is what separates the clause from the column."""
    assert validate_select("SELECT share FROM positions")
    with pytest.raises(SqlValidationError) as ei:
        validate_select("SELECT * FROM positions FOR SHARE")
    assert ei.value.detail["keyword"] == "FOR SHARE"


# --------------------------------------------------------------------------
# Lexing ambiguity (check 0)
#
# The bypass that motivated this check defeated every other check in the
# module at once, because every other check reasons about tokens and the
# tokens were wrong.
#
# sqlparse's string pattern is '(''|\\'|[^'])*', so a backslash-escaped quote
# keeps the string OPEN. PostgreSQL with standard_conforming_strings=on (the
# default since 9.1) treats backslash as an ordinary character, so the string
# CLOSES at that quote. SQLite agrees with PostgreSQL. Everything sqlparse
# believed was inert text inside a literal was live SQL to the engine.
#
# Verified end to end against PostgreSQL 16 with the service's own connect
# args: the first payload below returned the contents of /etc/hostname in the
# result set the API hands back to an unauthenticated caller, and the third
# executed a stacked pg_sleep(2).
# --------------------------------------------------------------------------

AMBIGUOUS_LEXING = [
    # Arbitrary server-side file read, smuggled into the FIRST statement so
    # its output lands in the result set the caller receives.
    r"SELECT 'x\', pg_read_file('/etc/hostname') AS leak, 'y'",
    r"SELECT 'x\', pg_ls_dir('/etc') AS leak, 'y'",
    # Statement stacking past the "exactly one statement" check.
    r"SELECT 'a\'; DROP TABLE users; --'",
    r"SELECT 'a\'; CREATE TABLE pwned(x int); --'",
    r"SELECT 'a\'; SELECT pg_sleep(2); --'",
    # A doubled backslash disagrees just as badly.
    r"SELECT 'a\\'; SELECT pg_sleep(2); --'",
    # PostgreSQL escape-strings, where even MySQL-style escaping is live.
    r"SELECT E'x\', pg_read_file('/etc/hostname') AS leak, 'y'",
    r"SELECT e'x\', pg_read_file('/etc/hostname') AS leak, 'y'",
    # Unicode-escaped identifiers: sqlparse does not decode them, so the name
    # reaching the blocklist is spelled differently from the function called.
    r'SELECT U&"pg_re\0061d_file"(1)',
    r'SELECT u&"pg_read_file"(1)',
    r"SELECT U&'\0061'",
]


@pytest.mark.parametrize("sql", AMBIGUOUS_LEXING)
def test_ambiguously_lexed_input_is_rejected(sql):
    with pytest.raises(SqlValidationError) as ei:
        validate_select(sql)
    assert ei.value.error_code == ErrorCode.INVALID_SQL


def test_the_exact_reported_exfiltration_payload_is_rejected():
    """Regression for the confirmed break, kept verbatim.

    Against PostgreSQL 16 this returned:
        columns ['?column?', 'leak', '?column?']
        rows    [['x\\', '<contents of /etc/hostname>', 'y']]
    """
    payload = r"SELECT 'x\', pg_read_file('/etc/hostname') AS leak, 'y'"
    with pytest.raises(SqlValidationError) as ei:
        validate_select(payload)
    assert ei.value.error_code == ErrorCode.INVALID_SQL


def test_the_check_runs_before_parsing():
    """It decides whether parsing means anything, so it cannot run after it.

    An ambiguous statement that is also too token-dense to parse cheaply must
    still be rejected on ambiguity, not spend the CPU first.
    """
    settings = get_settings()
    pairs = (settings.max_sql_length - 40) // 2
    sql = r"SELECT 'x\', " + ("a," * pairs) + "1"

    started = time.perf_counter()
    with pytest.raises(SqlValidationError) as ei:
        validate_select(sql)
    assert ei.value.error_code == ErrorCode.INVALID_SQL
    assert time.perf_counter() - started < 0.5


# Legitimate SQL that must survive check 0. A guard that rejects real analytics
# queries gets switched off, which is its own security failure.
UNAMBIGUOUS_BUT_BACKSLASHED = [
    # Regex with backslash classes: extremely common in fraud rules, and
    # unambiguous because no quote follows the backslash.
    r"SELECT * FROM t WHERE card ~ '^\d{16}$'",
    r"SELECT 'abc123' ~ '^[a-z]+\d+$' AS matched",
    r"SELECT regexp_replace(pan, '\s+', '') FROM cards",
    # Windows paths in data.
    r"SELECT path FROM files WHERE path LIKE 'C:\Users%'",
    # The portable spelling of a literal quote, unaffected.
    "SELECT 'It''s fine' AS note",
    # u& only matters as a literal prefix, not as a column or an operator.
    "SELECT u_id, u&x AS masked FROM t",
]


@pytest.mark.parametrize("sql", UNAMBIGUOUS_BUT_BACKSLASHED)
def test_unambiguous_backslashes_are_still_accepted(sql):
    assert validate_select(sql)


def test_mysql_executable_comment_is_still_destroyed_not_merely_allowed():
    """`/*! ... */` runs on MySQL and is inert everywhere else.

    The guard accepts the statement but returns it stripped, and the caller
    executes the returned string, so the payload never reaches any engine.
    """
    assert validate_select("SELECT 'x'; /*! SELECT pg_sleep(2) */") == "SELECT 'x'"
    assert validate_select("SELECT 1 /*!50000 UNION SELECT pg_sleep(5) */") == "SELECT 1"


def test_returned_string_is_itself_checked_for_ambiguity():
    """The caller executes the return value, so it gets the check too."""
    from app.security import sql_guard

    for sql in AMBIGUOUS_LEXING:
        with pytest.raises(SqlValidationError):
            validate_select(sql)
    # Nothing ambiguous was memoised as an acceptance.
    assert not any(sql_guard._BACKSLASH_QUOTE in key for key in sql_guard._validated)


# --------------------------------------------------------------------------
# PostgreSQL field notation: a call with no parenthesis
#
# `(x).f` and `x.f` both mean `f(x)` in PostgreSQL, which reaches a function
# without the `name` + `(` shape the function check keys on. Confirmed against
# PostgreSQL 16 through the service's own execution path:
#
#   SELECT ('/etc/hostname'::text).pg_read_file
#     -> ['pg_read_file'] [['9f9b00f4308e\n']]        (file contents)
#   SELECT ('/etc'::text).pg_ls_dir
#     -> ['pg_ls_dir'] [['.pwd.lock'], ['gai.conf'], ...]
#   SELECT (2.0::float8).pg_sleep                      (slept 2.02s)
# --------------------------------------------------------------------------

FIELD_NOTATION_CALLS = [
    "SELECT (2.0::float8).pg_sleep",
    "SELECT ('/etc/hostname'::text).pg_read_file",
    "SELECT ('/etc'::text).pg_ls_dir",
    "SELECT (('/etc/hostname')::text).pg_read_file",
    "SELECT text('/etc/hostname').pg_read_file",
    'SELECT (x)."pg_read_file" FROM t',
    "SELECT r.pg_read_file FROM (SELECT 1) r(x)",
    "SELECT (x).nextval FROM t",
    "SELECT (x).set_config FROM t",
    "SELECT (x).dblink FROM t",
]


@pytest.mark.parametrize("sql", FIELD_NOTATION_CALLS)
def test_field_notation_call_is_rejected(sql):
    with pytest.raises(SqlValidationError) as ei:
        validate_select(sql)
    assert ei.value.error_code == ErrorCode.FORBIDDEN_FUNCTION


def test_the_exact_field_notation_file_read_is_rejected():
    """Regression, kept verbatim. Returned /etc/hostname's contents on PG 16."""
    with pytest.raises(SqlValidationError) as ei:
        validate_select("SELECT ('/etc/hostname'::text).pg_read_file")
    assert ei.value.detail["function"] == "pg_read_file"


# The dot rule is scoped to PostgreSQL functions, because only PostgreSQL has
# field notation. Applying it to the whole blocklist would reject ordinary
# qualified column references.
QUALIFIED_COLUMNS_THAT_MUST_WORK = [
    "SELECT a.edit, b.comment FROM a JOIN b ON a.id = b.id",
    "SELECT t.sleep FROM naps t",
    "SELECT f.zipfile FROM files f",
    "SELECT t.benchmark FROM runs t",
    "SELECT t.readfile FROM t",
    "SELECT t.day, t.amount FROM txns t",
]


@pytest.mark.parametrize("sql", QUALIFIED_COLUMNS_THAT_MUST_WORK)
def test_qualified_column_named_after_a_non_postgres_function_still_works(sql):
    assert validate_select(sql)


def test_every_postgres_function_is_blocked_in_field_notation():
    """The whole PostgreSQL group, not just the ones that were demonstrated."""
    from app.security.sql_guard import _POSTGRES_FUNCTIONS

    for name in sorted(_POSTGRES_FUNCTIONS):
        with pytest.raises(SqlValidationError) as ei:
            validate_select(f"SELECT (x).{name} FROM t")
        assert ei.value.error_code == ErrorCode.FORBIDDEN_FUNCTION, name


def test_blocklist_groups_partition_the_whole_list():
    """The union must stay the full list, so regrouping cannot silently drop one."""
    from app.security.sql_guard import (
        _MYSQL_FUNCTIONS,
        _POSTGRES_FUNCTIONS,
        _SQLITE_FUNCTIONS,
    )

    assert (
        _POSTGRES_FUNCTIONS | _MYSQL_FUNCTIONS | _SQLITE_FUNCTIONS
    ) == sql_guard.FORBIDDEN_FUNCTIONS
