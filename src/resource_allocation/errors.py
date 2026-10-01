"""领域错误类型。

所有业务规则违反都抛出 DomainError 子类，HTTP 层据此映射状态码。
"""
from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """业务错误基类。"""

    status_code = 400
    error_code = "domain_error"

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.context = context

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.error_code, "message": str(self)}
        if self.context:
            payload["context"] = self.context
        return payload


class ValidationError(DomainError):
    status_code = 422
    error_code = "validation_error"


class NotFoundError(DomainError):
    status_code = 404
    error_code = "not_found"


class ConflictError(DomainError):
    status_code = 409
    error_code = "conflict"


class InvalidStateError(DomainError):
    status_code = 409
    error_code = "invalid_state"


class QuotaExceededError(DomainError):
    """年度额度 / 赛道保底额度不足。"""

    status_code = 422
    error_code = "quota_exceeded"


class ConstraintViolationError(DomainError):
    """违反限制条件（项目上限、赛道适用性等）。"""

    status_code = 422
    error_code = "constraint_violated"


class IdempotencyConflictError(DomainError):
    status_code = 409
    error_code = "idempotency_conflict"


class LedgerIntegrityError(DomainError):
    status_code = 422
    error_code = "ledger_integrity"
