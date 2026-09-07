"""统一错误码与异常（docs/02 §10.2）。"""
from __future__ import annotations


class ApiError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int | None = None,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code or status_for(code)
        self.retryable = retryable
        self.details = details or {}


STATUS_BY_CODE = {
    "AUTH_EXPIRED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "GONE": 410,
    "IDEMPOTENCY_CONFLICT": 409,
    "REVISION_CONFLICT": 409,
    "PAYLOAD_TOO_LARGE": 413,
    "STORAGE_QUOTA_EXCEEDED": 507,
    "SOURCE_BLOCKED": 422,
    "SUBTITLE_LOGIN_REQUIRED": 422,
    "SUBTITLE_NOT_FOUND": 422,
    "BUDGET_EXCEEDED": 402,
    "PROVIDER_AUTH_FAILED": 422,
    "PROVIDER_OUTCOME_UNKNOWN": 500,
    "SCHEMA_INVALID": 422,
    "CURSOR_EXPIRED": 410,
    "VERSION_UNSUPPORTED": 400,
    "RATE_LIMITED": 429,
}


def status_for(code: str, default: int = 400) -> int:
    return STATUS_BY_CODE.get(code, default)
