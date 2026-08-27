"""Error taxonomy.

Every failure the API can produce carries a machine-readable ``error_code`` and
a fixed HTTP status, so the frontend branches on the code rather than parsing
prose. The status lives on the ErrorCode itself, which keeps the mapping in one
place and makes "every code maps to exactly one status" testable.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    # --- 400: the request or its SQL is bad -------------------------------
    EMPTY_STATEMENT = "EMPTY_STATEMENT"
    MULTIPLE_STATEMENTS = "MULTIPLE_STATEMENTS"
    NON_SELECT_STATEMENT = "NON_SELECT_STATEMENT"
    FORBIDDEN_KEYWORD = "FORBIDDEN_KEYWORD"
    FORBIDDEN_FUNCTION = "FORBIDDEN_FUNCTION"
    INVALID_SQL = "INVALID_SQL"
    QUERY_EXECUTION_ERROR = "QUERY_EXECUTION_ERROR"
    INVALID_CHART_CONFIG = "INVALID_CHART_CONFIG"
    INVALID_CONNECTION_CONFIG = "INVALID_CONNECTION_CONFIG"
    DB_TLS_REQUIRED = "DB_TLS_REQUIRED"
    CONNECTION_PAUSED = "CONNECTION_PAUSED"
    ROW_LIMIT_EXCEEDED = "ROW_LIMIT_EXCEEDED"
    SQL_TOO_LONG = "SQL_TOO_LONG"
    RESULT_TOO_LARGE = "RESULT_TOO_LARGE"
    WEAK_PASSWORD = "WEAK_PASSWORD"

    # --- 401 / 403: the target DB refused us ------------------------------
    DB_AUTH_FAILED = "DB_AUTH_FAILED"
    DB_PERMISSION_DENIED = "DB_PERMISSION_DENIED"

    # --- 401 / 403: the *caller* is the problem ---------------------------
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    PASSWORD_CHANGE_REQUIRED = "PASSWORD_CHANGE_REQUIRED"
    FORBIDDEN = "FORBIDDEN"

    # --- 404 ---------------------------------------------------------------
    CONNECTION_NOT_FOUND = "CONNECTION_NOT_FOUND"
    QUERY_NOT_FOUND = "QUERY_NOT_FOUND"
    TABLE_NOT_FOUND = "TABLE_NOT_FOUND"
    DASHBOARD_NOT_FOUND = "DASHBOARD_NOT_FOUND"
    USER_NOT_FOUND = "USER_NOT_FOUND"

    # --- 409 ---------------------------------------------------------------
    DUPLICATE_NAME = "DUPLICATE_NAME"
    DUPLICATE_EMAIL = "DUPLICATE_EMAIL"
    #: 409 rather than 403: the caller is allowed to manage users in general,
    #: and this one request conflicts with the system's current state (nobody
    #: else could administer it if this change went through). A 403 would read
    #: as "you may not manage users", which is false and sends an admin
    #: hunting a permissions problem that does not exist. See
    #: ``user_service.guard_last_admin``.
    LAST_ADMIN = "LAST_ADMIN"

    # --- 422 ---------------------------------------------------------------
    REQUEST_VALIDATION_ERROR = "REQUEST_VALIDATION_ERROR"

    # --- 429 ---------------------------------------------------------------
    RATE_LIMITED = "RATE_LIMITED"

    # --- 5xx ---------------------------------------------------------------
    DB_UNREACHABLE = "DB_UNREACHABLE"
    QUERY_TIMEOUT = "QUERY_TIMEOUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_NOT_READY = "SERVICE_NOT_READY"


#: Single source of truth for code -> HTTP status. Tested for totality.
HTTP_STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.EMPTY_STATEMENT: 400,
    ErrorCode.MULTIPLE_STATEMENTS: 400,
    ErrorCode.NON_SELECT_STATEMENT: 400,
    ErrorCode.FORBIDDEN_KEYWORD: 400,
    ErrorCode.FORBIDDEN_FUNCTION: 400,
    ErrorCode.INVALID_SQL: 400,
    ErrorCode.QUERY_EXECUTION_ERROR: 400,
    ErrorCode.INVALID_CHART_CONFIG: 400,
    ErrorCode.INVALID_CONNECTION_CONFIG: 400,
    ErrorCode.DB_TLS_REQUIRED: 400,
    ErrorCode.CONNECTION_PAUSED: 409,
    ErrorCode.ROW_LIMIT_EXCEEDED: 400,
    ErrorCode.SQL_TOO_LONG: 400,
    ErrorCode.RESULT_TOO_LARGE: 400,
    ErrorCode.WEAK_PASSWORD: 400,
    ErrorCode.DB_AUTH_FAILED: 401,
    ErrorCode.DB_PERMISSION_DENIED: 403,
    ErrorCode.INVALID_CREDENTIALS: 401,
    ErrorCode.NOT_AUTHENTICATED: 401,
    ErrorCode.ACCOUNT_LOCKED: 403,
    ErrorCode.PASSWORD_CHANGE_REQUIRED: 403,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.CONNECTION_NOT_FOUND: 404,
    ErrorCode.QUERY_NOT_FOUND: 404,
    ErrorCode.TABLE_NOT_FOUND: 404,
    ErrorCode.DASHBOARD_NOT_FOUND: 404,
    ErrorCode.USER_NOT_FOUND: 404,
    ErrorCode.DUPLICATE_NAME: 409,
    ErrorCode.DUPLICATE_EMAIL: 409,
    ErrorCode.LAST_ADMIN: 409,
    ErrorCode.REQUEST_VALIDATION_ERROR: 422,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.DB_UNREACHABLE: 502,
    ErrorCode.QUERY_TIMEOUT: 504,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.SERVICE_NOT_READY: 503,
}


class AppError(Exception):
    """Base for every error the API converts into a structured response."""

    def __init__(
        self,
        error_code: ErrorCode,
        message: str,
        detail: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.detail = detail

    @property
    def http_status(self) -> int:
        return HTTP_STATUS_BY_CODE[self.error_code]

    def to_response(self) -> dict:
        return {
            "error_code": self.error_code.value,
            "message": self.message,
            "detail": self.detail,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}({self.error_code.value!r}, {self.message!r})"


class SqlValidationError(AppError):
    """The SQL guard refused the statement. Never reaches a target DB."""


class QueryExecutionError(AppError):
    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(ErrorCode.QUERY_EXECUTION_ERROR, message, detail)


class DbAuthError(AppError):
    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(ErrorCode.DB_AUTH_FAILED, message, detail)


class DbPermissionError(AppError):
    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(ErrorCode.DB_PERMISSION_DENIED, message, detail)


class DbUnreachableError(AppError):
    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(ErrorCode.DB_UNREACHABLE, message, detail)


class QueryTimeoutError(AppError):
    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(ErrorCode.QUERY_TIMEOUT, message, detail)


class NotFoundError(AppError):
    def __init__(
        self,
        error_code: ErrorCode,
        message: str,
        detail: dict | None = None,
    ) -> None:
        super().__init__(error_code, message, detail)


class DuplicateNameError(AppError):
    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(ErrorCode.DUPLICATE_NAME, message, detail)


class InvalidConfigError(AppError):
    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(ErrorCode.INVALID_CONNECTION_CONFIG, message, detail)


class ConnectionPausedError(AppError):
    """Someone paused this connection, so nothing may reach the target.

    409 rather than 400 or 503: the request is well formed and the database is
    presumably fine - it conflicts with a state a person deliberately put this
    connection into, and the fix is to reconnect rather than to change the
    request or wait.
    """

    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(ErrorCode.CONNECTION_PAUSED, message, detail)


class DbTlsRequiredError(AppError):
    """The target refused the connection because it was not encrypted.

    A configuration problem rather than an outage, so 400 rather than 502: the
    database is up and answering, it just will not talk in the clear. Separate
    from INVALID_CONNECTION_CONFIG because the fix is one specific field, and
    the message says which.
    """

    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(ErrorCode.DB_TLS_REQUIRED, message, detail)


class ResultTooLargeError(AppError):
    """The payload passed the byte budget while rows were being coerced."""

    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(ErrorCode.RESULT_TOO_LARGE, message, detail)


class RateLimitedError(AppError):
    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(ErrorCode.RATE_LIMITED, message, detail)


class ServiceNotReadyError(AppError):
    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(ErrorCode.SERVICE_NOT_READY, message, detail)
