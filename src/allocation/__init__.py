"""高校资源分类配置服务端。"""
from __future__ import annotations

from .api import make_server, run
from .errors import (
    DomainError,
    LineBlockedError,
    NotFoundError,
    QuotaError,
    StateError,
    ValidationError,
)
from .service import AllocationService

__all__ = [
    "AllocationService",
    "DomainError",
    "LineBlockedError",
    "NotFoundError",
    "QuotaError",
    "StateError",
    "ValidationError",
    "make_server",
    "run",
]
