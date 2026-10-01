"""领域模型与枚举。"""
from __future__ import annotations

import enum


class ResourceKind(str, enum.Enum):
    """资源类型：资金 / 实验条件 / 师资名额。"""

    FUND = "fund"            # 专项资金（整数分，避免浮点误差）
    LAB = "lab"              # 实验条件（套/间，整数）
    FACULTY = "faculty"      # 师资名额（人，整数）


class PlanStatus(str, enum.Enum):
    DRAFT = "draft"                 # 试算中，彼此隔离
    SUBMITTED = "submitted"         # 已提交，等待审批
    APPROVED = "approved"           # 审批通过，可下达
    ISSUED = "issued"               # 已正式下达（额度原子锁定，清单不可变）
    REJECTED = "rejected"           # 申请被退回（预占全部释放）
    PARTIALLY_RELEASED = "partially_released"  # 已下达后部分释放
    CLOSED = "closed"               # 已结清（全部释放或调剂完毕）


class BatchStatus(str, enum.Enum):
    """下达批次状态机，用于崩溃恢复。"""

    PENDING = "pending"     # 已登记，尚未开始执行
    RUNNING = "running"     # 执行中（含上次崩溃的残留批次）
    DONE = "done"           # 全部明细成功落账
    FAILED = "failed"       # 业务校验失败，整体回滚


class TxnType(str, enum.Enum):
    """台账分录类型。台账只追加、不可修改、不可删除。"""

    RESERVE = "reserve"                 # 下达时原子预占
    REJECT_RELEASE = "reject_release"   # 退回释放预占
    RELEASE = "release"                 # 下达后的部分释放（回收再用）
    WRITE_OFF = "write_off"             # 核销（已使用，不回收）
    TRANSFER_OUT = "transfer_out"       # 跨项目调剂划出
    TRANSFER_IN = "transfer_in"         # 跨项目调剂划入


class TransferStatus(str, enum.Enum):
    PENDING = "pending"
    DONE = "done"
    CANCELLED = "cancelled"
