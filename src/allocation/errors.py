"""服务端领域错误类型。

所有业务错误都携带稳定的错误码与可解释细节（details），
便于联合工作组追溯"为什么调减/为什么被拦"。
"""
from __future__ import annotations


class DomainError(Exception):
    """业务规则错误基类。"""

    http_status = 400
    code = "domain_error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class ValidationError(DomainError):
    """入参不合法。"""

    http_status = 400
    code = "validation_error"


class NotFoundError(DomainError):
    """目标资源不存在。"""

    http_status = 404
    code = "not_found"


class StateError(DomainError):
    """状态机不允许的操作（如重复审批、修改已下达清单）。"""

    http_status = 409
    code = "state_conflict"


class QuotaError(DomainError):
    """额度、保底比例或限制条件不满足。"""

    http_status = 409
    code = "quota_violation"


class LineBlockedError(QuotaError):
    """批次行在下达时被阻断（额度不足、重复占用、限制条件等）。"""

    code = "line_blocked"
