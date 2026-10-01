"""追加式台账：写入哈希链分录、查询占用、校验完整性。

每条分录带两个带符号增量：

====================  ==============  =====================
分录                  used_delta      outstanding_delta
====================  ==============  =====================
reserve（下达预占）   +amount         +amount
transfer_in（划入）   +amount         +amount
reject_release（退回）-amount         -amount
release（部分释放）   -amount         -amount
write_off（核销）     0               -amount
transfer_out（划出）  -amount         -amount
====================  ==============  =====================

- 账户已占用 = SUM(used_delta)；核销部分仍占用年度额度，不回收。
- 清单未结清额 = SUM(outstanding_delta)；可再释放/调剂的上限。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from .errors import LedgerIntegrityError


def utcnow() -> str:
    """统一的 UTC 时间戳（毫秒），便于排序与审计。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def begin_immediate(conn: sqlite3.Connection) -> None:
    """开启立即持写锁的事务，防止并发抢占超额。"""
    conn.execute("BEGIN IMMEDIATE")


def commit(conn: sqlite3.Connection) -> None:
    conn.execute("COMMIT")


def rollback(conn: sqlite3.Connection) -> None:
    conn.execute("ROLLBACK")


# 每种分录对 账户占用 / 清单未结清额 的带符号影响
_DELTAS = {
    "reserve":        (1, 1),
    "transfer_in":    (1, 1),
    "reject_release": (-1, -1),
    "release":        (-1, -1),
    "write_off":      (0, -1),
    "transfer_out":   (-1, -1),
}


def _hash_payload(prev_hash: str, fields: dict[str, Any]) -> str:
    blob = prev_hash + "|" + json.dumps(fields, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def append_entry(
    conn: sqlite3.Connection,
    *,
    plan_id: int | None,
    project_id: int,
    account_id: int,
    allocation_id: int,
    kind: str,
    track: str,
    txn_type: str,
    amount: int,
    ref: str,
    note: str = "",
    created_at: str | None = None,
) -> int:
    """追加一条台账分录。必须在事务内调用。

    每条分录携带同一账户链上前一条分录的哈希，形成只增哈希链。
    """
    du, do = _DELTAS[txn_type]
    used_delta = du * amount
    outstanding_delta = do * amount
    ts = created_at or utcnow()
    row = conn.execute(
        "SELECT entry_hash FROM ledger_entries WHERE account_id=? "
        "ORDER BY id DESC LIMIT 1",
        (account_id,),
    ).fetchone()
    prev_hash = row["entry_hash"] if row else ""
    fields = {
        "plan_id": plan_id,
        "project_id": project_id,
        "account_id": account_id,
        "allocation_id": allocation_id,
        "kind": kind,
        "track": track,
        "txn_type": txn_type,
        "amount": amount,
        "used_delta": used_delta,
        "outstanding_delta": outstanding_delta,
        "ref": ref,
        "note": note,
        "created_at": ts,
    }
    entry_hash = _hash_payload(prev_hash, fields)
    cur = conn.execute(
        """
        INSERT INTO ledger_entries
            (plan_id, project_id, account_id, allocation_id, kind, track,
             txn_type, amount, used_delta, outstanding_delta, ref, note,
             prev_hash, entry_hash, created_at)
        VALUES (:plan_id,:project_id,:account_id,:allocation_id,:kind,:track,
                :txn_type,:amount,:used_delta,:outstanding_delta,:ref,:note,
                :prev_hash,:entry_hash,:created_at)
        """,
        {**fields, "prev_hash": prev_hash, "entry_hash": entry_hash},
    )
    return int(cur.lastrowid)


def usage_by_track(conn: sqlite3.Connection, account_id: int) -> dict[str, int]:
    """返回账户按赛道汇总的当前占用量（含已核销部分）。"""
    rows = conn.execute(
        "SELECT track, COALESCE(SUM(used_delta),0) AS used "
        "FROM ledger_entries WHERE account_id=? GROUP BY track",
        (account_id,),
    ).fetchall()
    return {r["track"]: int(r["used"]) for r in rows}


def total_used(conn: sqlite3.Connection, account_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(used_delta),0) AS used "
        "FROM ledger_entries WHERE account_id=?",
        (account_id,),
    ).fetchone()
    return int(row["used"])


def project_used(conn: sqlite3.Connection, project_id: int, account_id: int) -> int:
    """某项目在某账户上的当前占用（用于限制每项目上限）。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(used_delta),0) AS used "
        "FROM ledger_entries WHERE project_id=? AND account_id=?",
        (project_id, account_id),
    ).fetchone()
    return int(row["used"])


def allocation_balance(conn: sqlite3.Connection, allocation_id: int) -> int:
    """单条不可变清单的未结清额（0 表示已释放/划出/核销完毕）。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(outstanding_delta),0) AS bal "
        "FROM ledger_entries WHERE allocation_id=?",
        (allocation_id,),
    ).fetchone()
    return int(row["bal"])


def allocation_events(
    conn: sqlite3.Connection, allocation_id: int
) -> Iterable[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM ledger_entries WHERE allocation_id=? ORDER BY id",
        (allocation_id,),
    ).fetchall()


def verify_chain(conn: sqlite3.Connection, account_id: int | None = None) -> None:
    """重算全库（或指定账户）哈希链，发现任何篡改即抛 LedgerIntegrityError。"""
    sql = "SELECT * FROM ledger_entries"
    params: tuple[Any, ...] = ()
    if account_id is not None:
        sql += " WHERE account_id=?"
        params = (account_id,)
    # 按账户分组、组内按 id 排序重放
    sql += " ORDER BY account_id, id"
    prev: dict[int, str] = {}
    for row in conn.execute(sql, params):
        expected_prev = prev.get(row["account_id"], "")
        if row["prev_hash"] != expected_prev:
            raise LedgerIntegrityError(
                "台账哈希链断裂：前序哈希不匹配",
                entry_id=row["id"],
                account_id=row["account_id"],
            )
        fields = {
            "plan_id": row["plan_id"],
            "project_id": row["project_id"],
            "account_id": row["account_id"],
            "allocation_id": row["allocation_id"],
            "kind": row["kind"],
            "track": row["track"],
            "txn_type": row["txn_type"],
            "amount": row["amount"],
            "used_delta": row["used_delta"],
            "outstanding_delta": row["outstanding_delta"],
            "ref": row["ref"],
            "note": row["note"],
            "created_at": row["created_at"],
        }
        if _hash_payload(expected_prev, fields) != row["entry_hash"]:
            raise LedgerIntegrityError(
                "台账哈希校验失败：分录内容可能被篡改",
                entry_id=row["id"],
                account_id=row["account_id"],
            )
        prev[row["account_id"]] = row["entry_hash"]


def account_snapshot(conn: sqlite3.Connection, account_id: int) -> dict[str, Any]:
    """账户账实快照：总额度、按赛道占用、可用量。"""
    acct = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if acct is None:
        raise LedgerIntegrityError("账户不存在", account_id=account_id)
    by_track = usage_by_track(conn, account_id)
    used = sum(by_track.values())
    return {
        "account_id": account_id,
        "year": acct["year"],
        "kind": acct["kind"],
        "name": acct["name"],
        "total_amount": acct["total_amount"],
        "used_amount": used,
        "available_amount": acct["total_amount"] - used,
        "used_by_track": by_track,
    }
