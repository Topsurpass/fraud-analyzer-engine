import pytest

from app.errors import (
    HTTP_STATUS_BY_CODE,
    AppError,
    DbAuthError,
    DbPermissionError,
    DbUnreachableError,
    DuplicateNameError,
    ErrorCode,
    NotFoundError,
    QueryTimeoutError,
    SqlValidationError,
)


def test_sql_validation_error_is_400():
    e = SqlValidationError(ErrorCode.MULTIPLE_STATEMENTS, "two statements")
    assert e.http_status == 400
    assert e.error_code == ErrorCode.MULTIPLE_STATEMENTS


@pytest.mark.parametrize(
    "exc,status",
    [
        (DbAuthError("no"), 401),
        (DbPermissionError("no"), 403),
        (DbUnreachableError("no"), 502),
        (QueryTimeoutError("slow"), 504),
        (DuplicateNameError("dupe"), 409),
        (NotFoundError(ErrorCode.QUERY_NOT_FOUND, "gone"), 404),
    ],
)
def test_status_mapping(exc, status):
    assert exc.http_status == status


def test_to_response_has_exactly_three_keys():
    body = SqlValidationError(ErrorCode.INVALID_SQL, "bad").to_response()
    assert set(body) == {"error_code", "message", "detail"}
    assert body["error_code"] == "INVALID_SQL"


def test_every_error_code_has_exactly_one_status():
    missing = set(ErrorCode) - set(HTTP_STATUS_BY_CODE)
    assert not missing, f"ErrorCode members with no HTTP status: {missing}"
    extra = set(HTTP_STATUS_BY_CODE) - set(ErrorCode)
    assert not extra


def test_app_error_is_an_exception():
    with pytest.raises(AppError):
        raise SqlValidationError(ErrorCode.INVALID_SQL, "boom")
