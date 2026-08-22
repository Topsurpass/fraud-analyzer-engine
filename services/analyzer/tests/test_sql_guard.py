"""Adversarial corpus for the SELECT-only guard.

This file is the quality gate for the highest-risk component in the service.
A single bypass here means the fraud tool is itself an attack surface.
"""

import pytest

from app.errors import ErrorCode, SqlValidationError
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
    # --- denial of service ---------------------------------------------------
    ("SELECT pg_sleep(30)", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT sleep(30)", ErrorCode.FORBIDDEN_FUNCTION),
    ("SELECT benchmark(100000000, md5('a'))", ErrorCode.FORBIDDEN_FUNCTION),
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
