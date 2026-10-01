"""SQLite 持久化：建账、批次与追加式台账。

设计要点：
- ``ledger_entries`` / ``allocations`` 只追加：触发器禁止 UPDATE/DELETE；
  台账带 SHA-256 哈希链，任何篡改可被 :func:`resource_allocation.ledger.verify_chain` 发现。
- 所有金额/数量均为正整数（资金单位：分），杜绝浮点误差。
- 写入方使用 BEGIN IMMEDIATE，避免并发抢占导致的额度超用。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 年度资源账户：按 年度 + 资源类型 建账
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY,
    year            INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    name            TEXT NOT NULL,
    total_amount    INTEGER NOT NULL CHECK (total_amount > 0),
    max_per_project INTEGER CHECK (max_per_project IS NULL OR max_per_project > 0),
    created_at      TEXT NOT NULL,
    UNIQUE (year, kind, name)
);

-- 账户的适用赛道与保底比例（基点，10000 = 100%）
CREATE TABLE IF NOT EXISTS account_tracks (
    account_id    INTEGER NOT NULL REFERENCES accounts(id),
    track         TEXT NOT NULL,
    guarantee_bps INTEGER NOT NULL CHECK (guarantee_bps BETWEEN 0 AND 10000),
    PRIMARY KEY (account_id, track)
);

CREATE TABLE IF NOT EXISTS projects (
    id         INTEGER PRIMARY KEY,
    code       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    track      TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id),
    year            INTEGER NOT NULL,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL,
    reject_reason   TEXT,
    idempotency_key TEXT UNIQUE,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- 试算明细：草案阶段完全不进台账，天然彼此隔离
CREATE TABLE IF NOT EXISTS plan_items (
    id           INTEGER PRIMARY KEY,
    plan_id      INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    amount       INTEGER NOT NULL CHECK (amount > 0),
    account_name TEXT,
    note         TEXT NOT NULL DEFAULT '',
    UNIQUE (plan_id, kind)
);

-- 下达/退回/释放批次：崩溃恢复的最小单元
CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY,
    idem_key    TEXT NOT NULL UNIQUE,
    plan_id     INTEGER NOT NULL REFERENCES plans(id),
    action      TEXT NOT NULL CHECK (action IN ('issue','reject','release')),
    reason      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL CHECK (status IN ('pending','running','done','failed')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    heartbeat   TEXT,
    created_at  TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS batch_items (
    id            INTEGER PRIMARY KEY,
    batch_id      INTEGER NOT NULL REFERENCES batches(id),
    seq           INTEGER NOT NULL,
    allocation_id INTEGER REFERENCES allocations(id),
    kind          TEXT,
    amount        INTEGER,
    account_name  TEXT,
    mode          TEXT CHECK (mode IS NULL OR
                  mode IN ('release','write_off','reject')),
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','done','failed')),
    error         TEXT,
    UNIQUE (batch_id, seq)
);

-- 不可变清单：每次锁定/划入生成一行，永不修改、永不删除
CREATE TABLE IF NOT EXISTS allocations (
    id         INTEGER PRIMARY KEY,
    batch_id   INTEGER REFERENCES batches(id),   -- 下达/退回/释放批次；调剂划入为空
    item_id    INTEGER REFERENCES batch_items(id),
    transfer_id INTEGER REFERENCES transfers(id),
    plan_id    INTEGER NOT NULL REFERENCES plans(id),
    project_id INTEGER NOT NULL REFERENCES projects(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    kind       TEXT NOT NULL,
    track      TEXT NOT NULL,
    amount     INTEGER NOT NULL CHECK (amount > 0),
    origin     TEXT NOT NULL CHECK (origin IN ('reserve','transfer_in')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alloc_plan ON allocations(plan_id);
CREATE INDEX IF NOT EXISTS idx_alloc_project ON allocations(project_id);
CREATE INDEX IF NOT EXISTS idx_alloc_account ON allocations(account_id);

-- 追加式台账（按账户哈希链串联的单边流水）
CREATE TABLE IF NOT EXISTS ledger_entries (
    id                  INTEGER PRIMARY KEY,
    plan_id             INTEGER REFERENCES plans(id),
    project_id          INTEGER NOT NULL REFERENCES projects(id),
    account_id          INTEGER NOT NULL REFERENCES accounts(id),
    allocation_id       INTEGER NOT NULL REFERENCES allocations(id),
    kind                TEXT NOT NULL,
    track               TEXT NOT NULL,
    txn_type            TEXT NOT NULL CHECK (txn_type IN
                          ('reserve','reject_release','release','write_off',
                           'transfer_out','transfer_in')),
    amount              INTEGER NOT NULL CHECK (amount > 0),
    used_delta          INTEGER NOT NULL,        -- 对账户占用量的带符号影响
    outstanding_delta   INTEGER NOT NULL,        -- 对清单未结清额的带符号影响
    ref                 TEXT NOT NULL,
    note                TEXT NOT NULL DEFAULT '',
    prev_hash           TEXT NOT NULL DEFAULT '',
    entry_hash          TEXT NOT NULL,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_account ON ledger_entries(account_id, kind);
CREATE INDEX IF NOT EXISTS idx_ledger_alloc ON ledger_entries(allocation_id);
CREATE INDEX IF NOT EXISTS idx_ledger_plan ON ledger_entries(plan_id);

-- 跨项目调剂
CREATE TABLE IF NOT EXISTS transfers (
    id           INTEGER PRIMARY KEY,
    idem_key     TEXT NOT NULL UNIQUE,
    from_plan_id INTEGER NOT NULL REFERENCES plans(id),
    to_plan_id   INTEGER NOT NULL REFERENCES plans(id),
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','done','cancelled')),
    reason       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    finished_at  TEXT
);

CREATE TABLE IF NOT EXISTS transfer_items (
    id                 INTEGER PRIMARY KEY,
    transfer_id        INTEGER NOT NULL REFERENCES transfers(id),
    seq                INTEGER NOT NULL,
    from_allocation_id INTEGER NOT NULL REFERENCES allocations(id),
    amount             INTEGER NOT NULL CHECK (amount > 0),
    UNIQUE (transfer_id, seq)
);

-- 台账与清单不可变
CREATE TRIGGER IF NOT EXISTS trg_ledger_no_update
BEFORE UPDATE ON ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'ledger_entries 是不可变台账，禁止更新');
END;
CREATE TRIGGER IF NOT EXISTS trg_ledger_no_delete
BEFORE DELETE ON ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'ledger_entries 是不可变台账，禁止删除');
END;
CREATE TRIGGER IF NOT EXISTS trg_alloc_no_update
BEFORE UPDATE ON allocations
BEGIN
    SELECT RAISE(ABORT, 'allocations 是不可变清单，禁止更新');
END;
CREATE TRIGGER IF NOT EXISTS trg_alloc_no_delete
BEFORE DELETE ON allocations
BEGIN
    SELECT RAISE(ABORT, 'allocations 是不可变清单，禁止删除');
END;
"""


def connect(path: str | Path = ":memory:", *, cross_thread: bool = False) -> sqlite3.Connection:
    """打开数据库连接并开启外键约束。

    cross_thread=True 时允许连接跨线程使用（HTTP 多线程场景，
    调用方需自行加锁；API 的 App 持有 RLock）。
    """
    conn = sqlite3.connect(
        str(path),
        timeout=30,
        isolation_level=None,
        check_same_thread=not cross_thread,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """初始化全部表与触发器，并记录 schema 版本。"""
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
