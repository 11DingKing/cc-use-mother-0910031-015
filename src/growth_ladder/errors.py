"""统一的领域错误类型，携带 HTTP 状态码便于接口层映射。"""
from __future__ import annotations


class GrowthError(Exception):
    """所有可预期业务错误的基类。"""

    status = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status

    def to_dto(self) -> dict:
        return {"error": self.code, "message": self.message}


class ValidationError(GrowthError):
    status = 400
    code = "validation_error"


class NotFoundError(GrowthError):
    status = 404
    code = "not_found"


class ConflictError(GrowthError):
    status = 409
    code = "conflict"


class RuleError(GrowthError):
    status = 422
    code = "rule_error"
