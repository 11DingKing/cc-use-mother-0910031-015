"""领域错误类型。"""
from __future__ import annotations


class DomainError(Exception):
    """业务规则错误，HTTP 层映射为 400。"""

    status = 400
    code = "domain_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class NotFoundError(DomainError):
    status = 404
    code = "not_found"


class ConflictError(DomainError):
    status = 409
    code = "conflict"


class LedgerUnbalancedError(DomainError):
    status = 500
    code = "ledger_unbalanced"
