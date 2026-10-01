"""SQLite 存储层：模式、连接与事务助手。

设计要点：

- 单写者串行化：所有写事务使用 ``BEGIN IMMEDIATE``，从根上避免并发重复扣减；
- 台账不可变：``ledger_entry`` 由触发器禁止 UPDATE/DELETE，清单只能冲正不能篡改；
- 幂等恢复：``batch_line.issue_key`` 与 ``ledger_entry.idempotency_key`` 唯一约束兜底，
  崩溃恢复后重放同一行只会命中约束而不会重复扣减；
- 重复占用防线：``(pool_id, application_id)`` 在在账占用上部分唯一，
  替代共享表格后从数据库层面杜绝同一申报在同一资源池重复占额度。
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (
  name  TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);

-- 资源池：按年度预算、适用赛道、保底比例、限制条件建账
CREATE TABLE IF NOT EXISTS resource_pool (
  pool_id        TEXT PRIMARY KEY,
  year           INTEGER NOT NULL,
  resource_type  TEXT NOT NULL CHECK (resource_type IN ('fund', 'lab', 'faculty')),
  tracks         TEXT NOT NULL,                 -- JSON 数组：适用赛道
  unit           TEXT NOT NULL,                 -- 计量单位（元/项/人）
  budget_amount  INTEGER NOT NULL CHECK (budget_amount >= 0),
  floor_ratio_bp INTEGER NOT NULL CHECK (floor_ratio_bp BETWEEN 0 AND 10000),  -- 保底比例（万分之一）
  restrictions   TEXT NOT NULL DEFAULT '{}',    -- JSON 对象：限制条件
  locked_amount  INTEGER NOT NULL DEFAULT 0 CHECK (locked_amount >= 0),
  version        INTEGER NOT NULL DEFAULT 0,
  created_by     TEXT NOT NULL,
  created_at     TEXT NOT NULL
);

-- 高校申报
CREATE TABLE IF NOT EXISTS application (
  application_id TEXT PRIMARY KEY,
  applicant      TEXT NOT NULL,                 -- 申报高校
  track          TEXT NOT NULL,                 -- 申报赛道
  title          TEXT NOT NULL,
  demands        TEXT NOT NULL DEFAULT '[]',    -- JSON 数组：需求明细
  status         TEXT NOT NULL DEFAULT 'submitted'
                 CHECK (status IN ('submitted', 'issued', 'partially_returned', 'returned')),
  created_by     TEXT NOT NULL,
  created_at     TEXT NOT NULL
);

-- 试算方案：与正式账完全隔离，只在本表与 plan_item 中留存
CREATE TABLE IF NOT EXISTS plan (
  plan_id    TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  year       INTEGER NOT NULL,
  status     TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'converted', 'abandoned')),
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_item (
  plan_id        TEXT NOT NULL REFERENCES plan(plan_id),
  application_id TEXT NOT NULL REFERENCES application(application_id),
  pool_id        TEXT NOT NULL REFERENCES resource_pool(pool_id),
  track          TEXT NOT NULL,
  amount         INTEGER NOT NULL CHECK (amount > 0),
  note           TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (plan_id, application_id, pool_id)
);

-- 下达批次：草稿 → 已审批 → 下达中 → 已下达（行失败则 failed，可调减后续办）
CREATE TABLE IF NOT EXISTS batch (
  batch_id    TEXT PRIMARY KEY,
  plan_id     TEXT REFERENCES plan(plan_id),
  year        INTEGER NOT NULL,
  status      TEXT NOT NULL DEFAULT 'draft'
              CHECK (status IN ('draft', 'approved', 'issuing', 'issued', 'failed')),
  fail_reason TEXT,
  created_by  TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  approved_by TEXT,
  approved_at TEXT,
  issued_at   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_batch_plan ON batch(plan_id) WHERE plan_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS batch_line (
  line_id        TEXT PRIMARY KEY,
  batch_id       TEXT NOT NULL REFERENCES batch(batch_id),
  seq            INTEGER NOT NULL,
  application_id TEXT NOT NULL REFERENCES application(application_id),
  pool_id        TEXT NOT NULL REFERENCES resource_pool(pool_id),
  track          TEXT NOT NULL,
  amount         INTEGER NOT NULL CHECK (amount > 0),
  status         TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'applied', 'failed')),
  fail_reason    TEXT,
  issue_key      TEXT NOT NULL UNIQUE,          -- 行级幂等键：恢复续办不重扣
  applied_at     TEXT,
  UNIQUE (batch_id, seq)
);

-- 在账占用（正式下达后形成，退回/释放/调剂只改这里与台账）
CREATE TABLE IF NOT EXISTS holding (
  holding_id     TEXT PRIMARY KEY,
  pool_id        TEXT NOT NULL REFERENCES resource_pool(pool_id),
  application_id TEXT NOT NULL REFERENCES application(application_id),
  batch_id       TEXT NOT NULL,                 -- 来源批次（调剂新建时为 REALLOC:*）
  line_id        TEXT NOT NULL UNIQUE,          -- 来源行（调剂新建时为 REALLOC:*）
  track          TEXT NOT NULL,
  amount         INTEGER NOT NULL CHECK (amount >= 0),
  status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'released')),
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
-- 同一资源池内同一申报只允许一笔在账占用，杜绝共享表格式的重复占用
CREATE UNIQUE INDEX IF NOT EXISTS uq_holding_active
  ON holding(pool_id, application_id) WHERE status = 'active';

-- 台账流水：只增不改，触发器保证不可变
CREATE TABLE IF NOT EXISTS ledger_entry (
  entry_id        TEXT PRIMARY KEY,
  pool_id         TEXT NOT NULL REFERENCES resource_pool(pool_id),
  application_id  TEXT,
  holding_id      TEXT,
  batch_id        TEXT,
  line_id         TEXT,
  action          TEXT NOT NULL CHECK (action IN ('lock', 'release', 'transfer_out', 'transfer_in')),
  amount          INTEGER NOT NULL CHECK (amount > 0),
  locked_after    INTEGER NOT NULL,             -- 记账后该池锁定余额
  reason          TEXT NOT NULL DEFAULT '',
  group_id        TEXT,                         -- 关联组：批次号或调剂号
  idempotency_key TEXT NOT NULL UNIQUE,
  actor           TEXT NOT NULL,
  created_at      TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS ledger_entry_no_update
BEFORE UPDATE ON ledger_entry BEGIN
  SELECT RAISE(ABORT, '台账不可变：禁止修改');
END;
CREATE TRIGGER IF NOT EXISTS ledger_entry_no_delete
BEFORE DELETE ON ledger_entry BEGIN
  SELECT RAISE(ABORT, '台账不可变：禁止删除');
END;

-- 调减留痕：每一次下达前调减都记录当时的可用/保底/锁定快照，保证决定可解释
CREATE TABLE IF NOT EXISTS adjustment (
  adjustment_id  TEXT PRIMARY KEY,
  batch_id       TEXT NOT NULL,
  line_id        TEXT NOT NULL,
  old_amount     INTEGER NOT NULL,
  new_amount     INTEGER NOT NULL,
  pool_available INTEGER NOT NULL,
  pool_floor     INTEGER NOT NULL,
  pool_locked    INTEGER NOT NULL,
  reason         TEXT NOT NULL,
  actor          TEXT NOT NULL,
  created_at     TEXT NOT NULL
);

-- 年度预算修订留痕
CREATE TABLE IF NOT EXISTS budget_revision (
  revision_id TEXT PRIMARY KEY,
  pool_id     TEXT NOT NULL REFERENCES resource_pool(pool_id),
  old_budget  INTEGER NOT NULL,
  new_budget  INTEGER NOT NULL,
  old_floor   INTEGER NOT NULL,
  new_floor   INTEGER NOT NULL,
  locked      INTEGER NOT NULL,
  reason      TEXT NOT NULL,
  actor       TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

-- 接口级幂等：同一 request_id 重试直接返回首个结果，不重复执行
CREATE TABLE IF NOT EXISTS idempotency (
  request_key TEXT PRIMARY KEY,
  endpoint    TEXT NOT NULL,
  response    TEXT NOT NULL,                    -- 'null' 表示已占位未完结（崩溃窗口）
  created_at  TEXT NOT NULL
);
"""


def utcnow() -> str:
    """返回 UTC 时间戳（ISO 8601，毫秒精度）。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Database:
    """线程局部连接的 SQLite 封装。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._memory = str(path) == ":memory:"
        if self._memory:
            # 命名共享内存库：同进程多线程（含 HTTP 工作线程）看到同一份数据
            self._dsn = f"file:alloc-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._uri = True
        else:
            self._dsn = str(path)
            self._uri = False
            Path(self._dsn).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._anchor = self._connect()  # 共享内存库的驻留连接
        self._anchor.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._dsn, uri=self._uri, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        if not self._memory:
            conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        """当前线程的连接（懒创建）。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    @contextmanager
    def txn(self) -> Iterator[sqlite3.Connection]:
        """写事务：BEGIN IMMEDIATE 串行化写者，异常自动回滚。"""
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    def next_id(self, conn: sqlite3.Connection, prefix: str) -> str:
        """在事务内生成单调递增的人类可读编号。"""
        conn.execute(
            "INSERT INTO counters(name, value) VALUES (?, 0) ON CONFLICT(name) DO NOTHING",
            (prefix,),
        )
        conn.execute("UPDATE counters SET value = value + 1 WHERE name = ?", (prefix,))
        row = conn.execute("SELECT value FROM counters WHERE name = ?", (prefix,)).fetchone()
        return f"{prefix}-{row['value']:04d}"

    def close(self) -> None:
        self._anchor.close()
