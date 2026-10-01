"""高校资源分类配置核心服务。

覆盖领域契约（domain/contract.json）中的关键不变量：

- 年度额度：资源池按年度预算建账，一切占用受 ``budget_amount`` 硬约束；
- 原子占用：每条下达行在独立事务内完成"校验 + 占用 + 台账"，要么全部成功要么全部回滚；
- 方案比较：试算方案只读投影真实账目，方案之间、方案与正式账完全隔离；
- 恢复续办：行级幂等键 + 批次状态机，崩溃后 ``recover`` 续办不会重复扣减。

账实一致的口径：``resource_pool.locked_amount``
永远等于在账占用之和，也永远等于台账有符号流水之和；``reconcile`` 随时可验证。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .db import Database, utcnow
from .errors import (
    LineBlockedError,
    NotFoundError,
    QuotaError,
    StateError,
    ValidationError,
)

RESOURCE_TYPES = ("fund", "lab", "faculty")
RESOURCE_TYPE_NAMES = {"fund": "专项资金", "lab": "实验条件", "faculty": "师资名额"}

# 限制条件（restrictions）中由系统强制执行的键，其余键原样留存作元数据
MAX_SINGLE_AMOUNT = "max_single_amount"      # 单项上限
PER_APPLICANT_CAP = "per_applicant_cap"      # 同一高校在该池的在账上限
ALLOWED_APPLICANTS = "allowed_applicants"    # 允许承接的高校名单

_INSERT_HOLDING = """
INSERT INTO holding (holding_id, pool_id, application_id, batch_id, line_id, track, amount, status, created_at, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
"""

_INSERT_LEDGER = """
INSERT INTO ledger_entry
  (entry_id, pool_id, application_id, holding_id, batch_id, line_id, action, amount, locked_after, reason, group_id, idempotency_key, actor, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _row_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


class AllocationService:
    """高校资源分类配置服务：建账、试算、下达、退回、调剂、对账与恢复。"""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db = Database(db_path)

    # ------------------------------------------------------------------
    # 基础校验与行读取
    # ------------------------------------------------------------------
    @staticmethod
    def _require_int(value: Any, field: str, *, minimum: int | None = None) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{field} 必须是整数", details={"field": field, "value": value})
        if minimum is not None and value < minimum:
            raise ValidationError(
                f"{field} 不能小于 {minimum}", details={"field": field, "value": value}
            )
        return value

    @staticmethod
    def _require_str(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{field} 必须是非空字符串", details={"field": field})
        return value.strip()

    @staticmethod
    def _normalize_floor_ratio(floor_ratio_bp: Any, floor_ratio: Any) -> int:
        """保底比例统一归一为万分比整数，避免浮点误差。"""
        if floor_ratio_bp is not None and floor_ratio is not None:
            raise ValidationError("floor_ratio_bp 与 floor_ratio 只能提供一个")
        if floor_ratio is not None:
            if isinstance(floor_ratio, bool) or not isinstance(floor_ratio, (int, float)):
                raise ValidationError("floor_ratio 必须在 [0, 1] 之间")
            if not 0 <= floor_ratio <= 1:
                raise ValidationError("floor_ratio 必须在 [0, 1] 之间")
            floor_ratio_bp = round(floor_ratio * 10000)
        if floor_ratio_bp is None:
            return 0
        bp = AllocationService._require_int(floor_ratio_bp, "floor_ratio_bp", minimum=0)
        if bp > 10000:
            raise ValidationError("floor_ratio_bp 必须在 [0, 10000] 之间（万分之一）")
        return bp

    @staticmethod
    def _get_pool_row(conn: sqlite3.Connection, pool_id: Any) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM resource_pool WHERE pool_id = ?", (pool_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("资源池不存在", details={"pool_id": pool_id})
        return row

    @staticmethod
    def _get_application_row(conn: sqlite3.Connection, application_id: Any) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM application WHERE application_id = ?", (application_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("申报不存在", details={"application_id": application_id})
        return row

    @staticmethod
    def _get_plan_row(conn: sqlite3.Connection, plan_id: Any) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM plan WHERE plan_id = ?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("试算方案不存在", details={"plan_id": plan_id})
        return row

    @staticmethod
    def _get_batch_row(conn: sqlite3.Connection, batch_id: Any) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM batch WHERE batch_id = ?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("批次不存在", details={"batch_id": batch_id})
        return row

    # ------------------------------------------------------------------
    # 视图
    # ------------------------------------------------------------------
    @staticmethod
    def _pool_view(row: sqlite3.Row) -> dict:
        floor_amount = row["budget_amount"] * row["floor_ratio_bp"] // 10000
        locked = row["locked_amount"]
        return {
            "pool_id": row["pool_id"],
            "year": row["year"],
            "resource_type": row["resource_type"],
            "resource_type_name": RESOURCE_TYPE_NAMES[row["resource_type"]],
            "tracks": json.loads(row["tracks"]),
            "unit": row["unit"],
            "budget_amount": row["budget_amount"],
            "floor_ratio_bp": row["floor_ratio_bp"],
            "floor_amount": floor_amount,
            "restrictions": json.loads(row["restrictions"]),
            "locked_amount": locked,
            "available_amount": row["budget_amount"] - locked,
            "transferable_amount": max(0, locked - floor_amount),
            "version": row["version"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    def _application_view(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
        holdings = conn.execute(
            "SELECT * FROM holding WHERE application_id = ? ORDER BY created_at",
            (row["application_id"],),
        ).fetchall()
        return {
            "application_id": row["application_id"],
            "applicant": row["applicant"],
            "track": row["track"],
            "title": row["title"],
            "demands": json.loads(row["demands"]),
            "status": row["status"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "holdings": [_row_dict(h) for h in holdings],
        }

    def _plan_view(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
        items = conn.execute(
            """SELECT pi.*, a.applicant AS applicant
               FROM plan_item pi JOIN application a ON a.application_id = pi.application_id
               WHERE pi.plan_id = ? ORDER BY pi.application_id, pi.pool_id""",
            (row["plan_id"],),
        ).fetchall()
        return {
            "plan_id": row["plan_id"],
            "name": row["name"],
            "year": row["year"],
            "status": row["status"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "items": [_row_dict(it) for it in items],
        }

    def _batch_view(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
        lines = conn.execute(
            "SELECT * FROM batch_line WHERE batch_id = ? ORDER BY seq", (row["batch_id"],)
        ).fetchall()
        return {
            "batch_id": row["batch_id"],
            "plan_id": row["plan_id"],
            "year": row["year"],
            "status": row["status"],
            "fail_reason": row["fail_reason"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "approved_by": row["approved_by"],
            "approved_at": row["approved_at"],
            "issued_at": row["issued_at"],
            "lines": [_row_dict(line) for line in lines],
        }

    # ------------------------------------------------------------------
    # 接口级幂等
    # ------------------------------------------------------------------
    def _idempotency_begin(
        self, conn: sqlite3.Connection, request_id: str | None, endpoint: str
    ) -> dict | None:
        """在事务内占位；若键已有完结结果则直接返回（重放安全）。"""
        if request_id is None:
            return None
        self._require_str(request_id, "request_id")
        row = conn.execute(
            "SELECT endpoint, response FROM idempotency WHERE request_key = ?", (request_id,)
        ).fetchone()
        if row is not None:
            if row["endpoint"] != endpoint:
                raise ValidationError(
                    "request_id 已被其他接口占用",
                    details={"request_id": request_id, "endpoint": row["endpoint"]},
                )
            stored = json.loads(row["response"])
            if stored is None:
                return None  # 崩溃时只完成了占位：继续执行并在收尾覆盖
            stored = dict(stored)
            stored["replayed"] = True
            return stored
        conn.execute(
            "INSERT INTO idempotency (request_key, endpoint, response, created_at) VALUES (?, ?, 'null', ?)",
            (request_id, endpoint, utcnow()),
        )
        return None

    @staticmethod
    def _idempotency_finish(
        conn: sqlite3.Connection, request_id: str | None, result: dict
    ) -> None:
        if request_id is not None:
            conn.execute(
                "UPDATE idempotency SET response = ? WHERE request_key = ?",
                (json.dumps(result, ensure_ascii=False, sort_keys=True), request_id),
            )

    # ------------------------------------------------------------------
    # 建账：资源池
    # ------------------------------------------------------------------
    def create_pool(
        self,
        *,
        year: int,
        resource_type: str,
        tracks: list,
        unit: str,
        budget_amount: int,
        floor_ratio_bp: int | None = None,
        floor_ratio: float | None = None,
        restrictions: dict | None = None,
        actor: str,
        **_: Any,
    ) -> dict:
        """按年度预算、适用赛道、保底比例、限制条件建立资源池账户。"""
        self._require_int(year, "year", minimum=2000)
        if resource_type not in RESOURCE_TYPES:
            raise ValidationError(
                "resource_type 必须是 fund/lab/faculty 之一",
                details={"resource_type": resource_type},
            )
        if (
            not isinstance(tracks, list)
            or not tracks
            or not all(isinstance(t, str) and t.strip() for t in tracks)
        ):
            raise ValidationError("tracks 必须是非空字符串数组（适用赛道）")
        tracks = sorted({t.strip() for t in tracks})
        unit = self._require_str(unit, "unit")
        self._require_int(budget_amount, "budget_amount", minimum=0)
        bp = self._normalize_floor_ratio(floor_ratio_bp, floor_ratio)
        if restrictions is None:
            restrictions = {}
        if not isinstance(restrictions, dict):
            raise ValidationError("restrictions 必须是对象（限制条件）")
        actor = self._require_str(actor, "actor")
        with self.db.txn() as conn:
            pool_id = self.db.next_id(conn, "POOL")
            conn.execute(
                """INSERT INTO resource_pool
                   (pool_id, year, resource_type, tracks, unit, budget_amount, floor_ratio_bp,
                    restrictions, locked_amount, version, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)""",
                (
                    pool_id,
                    year,
                    resource_type,
                    json.dumps(tracks, ensure_ascii=False),
                    unit,
                    budget_amount,
                    bp,
                    json.dumps(restrictions, ensure_ascii=False, sort_keys=True),
                    actor,
                    utcnow(),
                ),
            )
            return self._pool_view(self._get_pool_row(conn, pool_id))

    def get_pool(self, pool_id: str) -> dict:
        return self._pool_view(self._get_pool_row(self.db.conn, pool_id))

    def list_pools(self, year: int | None = None, resource_type: str | None = None) -> list:
        sql = "SELECT * FROM resource_pool"
        clauses, args = [], []
        if year is not None:
            clauses.append("year = ?")
            args.append(year)
        if resource_type is not None:
            clauses.append("resource_type = ?")
            args.append(resource_type)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY pool_id"
        rows = self.db.conn.execute(sql, args).fetchall()
        return [self._pool_view(row) for row in rows]

    def pool_holdings(self, pool_id: str) -> list:
        conn = self.db.conn
        self._get_pool_row(conn, pool_id)
        rows = conn.execute(
            "SELECT * FROM holding WHERE pool_id = ? ORDER BY created_at", (pool_id,)
        ).fetchall()
        return [_row_dict(row) for row in rows]

    def pool_ledger(self, pool_id: str) -> list:
        conn = self.db.conn
        self._get_pool_row(conn, pool_id)
        rows = conn.execute(
            "SELECT * FROM ledger_entry WHERE pool_id = ? ORDER BY rowid", (pool_id,)
        ).fetchall()
        return [_row_dict(row) for row in rows]

    def revise_budget(
        self, *, pool_id: str, new_budget: int, reason: str, actor: str, **_: Any
    ) -> dict:
        """修订年度预算：不得低于已锁定额度，全程留痕保证调减可解释。"""
        self._require_int(new_budget, "new_budget", minimum=0)
        reason = self._require_str(reason, "reason")
        actor = self._require_str(actor, "actor")
        with self.db.txn() as conn:
            pool = self._get_pool_row(conn, pool_id)
            if new_budget < pool["locked_amount"]:
                raise QuotaError(
                    "新预算不得低于已锁定额度",
                    details={
                        "locked_amount": pool["locked_amount"],
                        "new_budget": new_budget,
                    },
                )
            old_floor = pool["budget_amount"] * pool["floor_ratio_bp"] // 10000
            new_floor = new_budget * pool["floor_ratio_bp"] // 10000
            revision_id = self.db.next_id(conn, "REV")
            conn.execute(
                """INSERT INTO budget_revision
                   (revision_id, pool_id, old_budget, new_budget, old_floor, new_floor,
                    locked, reason, actor, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    revision_id,
                    pool_id,
                    pool["budget_amount"],
                    new_budget,
                    old_floor,
                    new_floor,
                    pool["locked_amount"],
                    reason,
                    actor,
                    utcnow(),
                ),
            )
            conn.execute(
                "UPDATE resource_pool SET budget_amount = ?, version = version + 1 WHERE pool_id = ?",
                (new_budget, pool_id),
            )
            return self._pool_view(self._get_pool_row(conn, pool_id))

    # ------------------------------------------------------------------
    # 申报
    # ------------------------------------------------------------------
    def create_application(
        self,
        *,
        applicant: str,
        track: str,
        title: str,
        demands: list | None = None,
        actor: str,
        **_: Any,
    ) -> dict:
        applicant = self._require_str(applicant, "applicant")
        track = self._require_str(track, "track")
        title = self._require_str(title, "title")
        actor = self._require_str(actor, "actor")
        if demands is None:
            demands = []
        if not isinstance(demands, list):
            raise ValidationError("demands 必须是数组")
        normalized = []
        for item in demands:
            if not isinstance(item, dict):
                raise ValidationError("demands 条目必须是对象")
            resource_type = item.get("resource_type")
            if resource_type not in RESOURCE_TYPES:
                raise ValidationError(
                    "demands.resource_type 必须是 fund/lab/faculty 之一",
                    details={"resource_type": resource_type},
                )
            amount = self._require_int(item.get("amount"), "demands.amount", minimum=1)
            normalized.append({"resource_type": resource_type, "amount": amount})
        with self.db.txn() as conn:
            application_id = self.db.next_id(conn, "APP")
            conn.execute(
                """INSERT INTO application
                   (application_id, applicant, track, title, demands, status, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, 'submitted', ?, ?)""",
                (
                    application_id,
                    applicant,
                    track,
                    title,
                    json.dumps(normalized, ensure_ascii=False),
                    actor,
                    utcnow(),
                ),
            )
            return self._application_view(conn, self._get_application_row(conn, application_id))

    def get_application(self, application_id: str) -> dict:
        conn = self.db.conn
        return self._application_view(conn, self._get_application_row(conn, application_id))

    # ------------------------------------------------------------------
    # 试算方案（与正式账隔离）
    # ------------------------------------------------------------------
    def create_plan(self, *, name: str, year: int, actor: str, **_: Any) -> dict:
        name = self._require_str(name, "name")
        self._require_int(year, "year", minimum=2000)
        actor = self._require_str(actor, "actor")
        with self.db.txn() as conn:
            plan_id = self.db.next_id(conn, "PLAN")
            conn.execute(
                "INSERT INTO plan (plan_id, name, year, status, created_by, created_at) VALUES (?, ?, ?, 'draft', ?, ?)",
                (plan_id, name, year, actor, utcnow()),
            )
            return self._plan_view(conn, self._get_plan_row(conn, plan_id))

    def get_plan(self, plan_id: str) -> dict:
        conn = self.db.conn
        return self._plan_view(conn, self._get_plan_row(conn, plan_id))

    def add_plan_item(
        self,
        *,
        plan_id: str,
        application_id: str,
        pool_id: str,
        amount: int,
        note: str = "",
        actor: str | None = None,
        **_: Any,
    ) -> dict:
        """向试算方案加入条目；同一申请在同一池重复添加将覆盖数量。"""
        self._require_int(amount, "amount", minimum=1)
        with self.db.txn() as conn:
            plan = self._get_plan_row(conn, plan_id)
            if plan["status"] != "draft":
                raise StateError(
                    "方案已转换或废弃，不能再修改", details={"status": plan["status"]}
                )
            app = self._get_application_row(conn, application_id)
            pool = self._get_pool_row(conn, pool_id)
            if pool["year"] != plan["year"]:
                raise ValidationError(
                    "资源池年度与方案年度不一致",
                    details={"pool_year": pool["year"], "plan_year": plan["year"]},
                )
            tracks = json.loads(pool["tracks"])
            if app["track"] not in tracks:
                raise ValidationError(
                    "申报赛道不适用于该资源池",
                    details={"track": app["track"], "tracks": tracks},
                )
            conn.execute(
                """INSERT INTO plan_item (plan_id, application_id, pool_id, track, amount, note)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(plan_id, application_id, pool_id)
                   DO UPDATE SET amount = excluded.amount, note = excluded.note, track = excluded.track""",
                (plan_id, application_id, pool_id, app["track"], amount, note or ""),
            )
            return self._plan_view(conn, self._get_plan_row(conn, plan_id))

    def simulate_plan(self, plan_id: str) -> dict:
        """试算：只读投影真实账目，不写任何正式账，方案彼此隔离。"""
        with self.db.txn() as conn:
            plan = self._get_plan_row(conn, plan_id)
            items = conn.execute(
                """SELECT pi.*, a.applicant AS applicant
                   FROM plan_item pi JOIN application a ON a.application_id = pi.application_id
                   WHERE pi.plan_id = ? ORDER BY pi.application_id, pi.pool_id""",
                (plan_id,),
            ).fetchall()
            planned_by_pool_applicant: dict[tuple, int] = {}
            for item in items:
                key = (item["pool_id"], item["applicant"])
                planned_by_pool_applicant[key] = (
                    planned_by_pool_applicant.get(key, 0) + item["amount"]
                )
            pools: dict[str, dict] = {}
            item_reports = []
            feasible = True
            for item in items:
                pool = self._get_pool_row(conn, item["pool_id"])
                entry = pools.setdefault(
                    pool["pool_id"],
                    {
                        "pool_id": pool["pool_id"],
                        "budget_amount": pool["budget_amount"],
                        "locked_amount": pool["locked_amount"],
                        "floor_amount": pool["budget_amount"] * pool["floor_ratio_bp"] // 10000,
                        "plan_amount": 0,
                        "projected_available": 0,
                        "overcommitted": False,
                    },
                )
                entry["plan_amount"] += item["amount"]
                violations = self._restriction_violations(
                    conn,
                    pool,
                    applicant=item["applicant"],
                    amount=item["amount"],
                    planned_total=planned_by_pool_applicant[(item["pool_id"], item["applicant"])],
                )
                if item["track"] not in json.loads(pool["tracks"]):
                    violations.append("赛道不适用")
                if violations:
                    feasible = False
                item_reports.append(
                    {
                        "application_id": item["application_id"],
                        "applicant": item["applicant"],
                        "pool_id": item["pool_id"],
                        "track": item["track"],
                        "amount": item["amount"],
                        "violations": violations,
                    }
                )
            pool_reports = []
            for entry in pools.values():
                projected = entry["budget_amount"] - entry["locked_amount"] - entry["plan_amount"]
                entry["projected_available"] = projected
                entry["overcommitted"] = projected < 0
                if entry["overcommitted"]:
                    feasible = False
                pool_reports.append(entry)
            return {
                "plan_id": plan["plan_id"],
                "name": plan["name"],
                "year": plan["year"],
                "status": plan["status"],
                "feasible": feasible,
                "pools": pool_reports,
                "items": item_reports,
            }

    def compare_plans(self, plan_ids: list) -> dict:
        """方案比较：多个试算方案在同一资源池上的占用投影对照。"""
        if not isinstance(plan_ids, list) or not plan_ids:
            raise ValidationError("plan_ids 必须是非空数组")
        if len(plan_ids) > 10:
            raise ValidationError("一次最多比较 10 个方案")
        simulations = [self.simulate_plan(plan_id) for plan_id in plan_ids]
        pools: dict[str, dict] = {}
        for sim in simulations:
            for entry in sim["pools"]:
                row = pools.setdefault(
                    entry["pool_id"],
                    {
                        "pool_id": entry["pool_id"],
                        "budget_amount": entry["budget_amount"],
                        "locked_amount": entry["locked_amount"],
                        "plan_amounts": {},
                        "projected_available": {},
                    },
                )
                row["plan_amounts"][sim["plan_id"]] = entry["plan_amount"]
                row["projected_available"][sim["plan_id"]] = entry["projected_available"]
        return {
            "plans": [
                {"plan_id": s["plan_id"], "name": s["name"], "feasible": s["feasible"]}
                for s in simulations
            ],
            "pools": list(pools.values()),
        }

    # ------------------------------------------------------------------
    # 下达批次
    # ------------------------------------------------------------------
    def create_batch(
        self,
        *,
        actor: str,
        plan_id: str | None = None,
        year: int | None = None,
        lines: list | None = None,
        **_: Any,
    ) -> dict:
        """建立下达批次：从试算方案整体转换，或直接给定明细行。"""
        actor = self._require_str(actor, "actor")
        with self.db.txn() as conn:
            if plan_id is not None:
                if lines is not None or year is not None:
                    raise ValidationError("plan_id 与 year/lines 互斥：从方案建批次时不要重复传明细")
                plan = self._get_plan_row(conn, plan_id)
                if plan["status"] != "draft":
                    raise StateError(
                        "方案已转换或废弃，不能重复建批次",
                        details={"status": plan["status"]},
                    )
                items = conn.execute(
                    "SELECT * FROM plan_item WHERE plan_id = ? ORDER BY application_id, pool_id",
                    (plan_id,),
                ).fetchall()
                if not items:
                    raise ValidationError("方案没有任何条目，无法建批次")
                year = plan["year"]
                specs = [
                    {
                        "application_id": it["application_id"],
                        "pool_id": it["pool_id"],
                        "amount": it["amount"],
                    }
                    for it in items
                ]
            else:
                year = self._require_int(year, "year", minimum=2000)
                if not isinstance(lines, list) or not lines:
                    raise ValidationError("lines 必须是非空数组")
                specs = lines
            batch_id = self.db.next_id(conn, "BATCH")
            conn.execute(
                "INSERT INTO batch (batch_id, plan_id, year, status, created_by, created_at) VALUES (?, ?, ?, 'draft', ?, ?)",
                (batch_id, plan_id, year, actor, utcnow()),
            )
            if plan_id is not None:
                conn.execute("UPDATE plan SET status = 'converted' WHERE plan_id = ?", (plan_id,))
            for seq, spec in enumerate(specs, start=1):
                if not isinstance(spec, dict):
                    raise ValidationError("批次行必须是对象")
                application_id = spec.get("application_id")
                pool_id = spec.get("pool_id")
                if not application_id or not pool_id:
                    raise ValidationError("批次行必须包含 application_id 与 pool_id")
                app = self._get_application_row(conn, application_id)
                pool = self._get_pool_row(conn, pool_id)
                amount = self._require_int(spec.get("amount"), "amount", minimum=1)
                if pool["year"] != year:
                    raise ValidationError(
                        "资源池年度与批次年度不一致",
                        details={"pool_year": pool["year"], "batch_year": year},
                    )
                tracks = json.loads(pool["tracks"])
                if app["track"] not in tracks:
                    raise ValidationError(
                        "申报赛道不适用于资源池",
                        details={"track": app["track"], "tracks": tracks},
                    )
                line_id = self.db.next_id(conn, "LINE")
                conn.execute(
                    """INSERT INTO batch_line
                       (line_id, batch_id, seq, application_id, pool_id, track, amount, status, issue_key)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                    (
                        line_id,
                        batch_id,
                        seq,
                        app["application_id"],
                        pool["pool_id"],
                        app["track"],
                        amount,
                        f"{batch_id}:{line_id}",
                    ),
                )
            return self._batch_view(conn, self._get_batch_row(conn, batch_id))

    def get_batch(self, batch_id: str) -> dict:
        conn = self.db.conn
        return self._batch_view(conn, self._get_batch_row(conn, batch_id))

    def list_batches(self, status: str | None = None) -> list:
        conn = self.db.conn
        if status:
            rows = conn.execute(
                "SELECT * FROM batch WHERE status = ? ORDER BY created_at", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM batch ORDER BY created_at").fetchall()
        return [_row_dict(row) for row in rows]

    def approve_batch(self, *, batch_id: str, actor: str, **_: Any) -> dict:
        actor = self._require_str(actor, "actor")
        with self.db.txn() as conn:
            batch = self._get_batch_row(conn, batch_id)
            if batch["status"] != "draft":
                raise StateError(
                    "仅草稿状态的批次可以审批", details={"status": batch["status"]}
                )
            conn.execute(
                "UPDATE batch SET status = 'approved', approved_by = ?, approved_at = ? WHERE batch_id = ?",
                (actor, utcnow(), batch_id),
            )
            return self._batch_view(conn, self._get_batch_row(conn, batch_id))

    def issue_batch(
        self, *, batch_id: str, actor: str, request_id: str | None = None, **_: Any
    ) -> dict:
        """正式下达：逐行原子锁定额度；失败即停可续办，崩溃恢复不会重复扣减。"""
        actor = self._require_str(actor, "actor")
        # 1) 幂等占位 + 状态推进（首个事务）
        with self.db.txn() as conn:
            cached = self._idempotency_begin(conn, request_id, "issue_batch")
            if cached is not None:
                return cached
            batch = self._get_batch_row(conn, batch_id)
            if batch["status"] == "issued":
                result = {
                    "batch_id": batch_id,
                    "status": "issued",
                    "applied_lines": [],
                    "note": "批次已下达，重复请求被安全忽略",
                    "manifest_digest": self._manifest(conn, batch)["digest"],
                }
                self._idempotency_finish(conn, request_id, result)
                return result
            if batch["status"] not in ("approved", "issuing", "failed"):
                raise StateError(
                    "批次未审批，不能下达", details={"status": batch["status"]}
                )
            conn.execute(
                "UPDATE batch SET status = 'issuing', fail_reason = NULL WHERE batch_id = ?",
                (batch_id,),
            )
        # 2) 逐行原子下达：每行一个事务，崩溃后未完成的行保持 pending
        applied: list[str] = []
        failure: dict | None = None
        while True:
            row = self.db.conn.execute(
                "SELECT line_id FROM batch_line WHERE batch_id = ? AND status != 'applied' ORDER BY seq LIMIT 1",
                (batch_id,),
            ).fetchone()
            if row is None:
                break
            line_id = row["line_id"]
            try:
                with self.db.txn() as conn:
                    self._apply_line(conn, batch_id, line_id, actor)
                applied.append(line_id)
            except LineBlockedError as exc:
                with self.db.txn() as conn:
                    conn.execute(
                        "UPDATE batch_line SET status = 'failed', fail_reason = ? WHERE line_id = ?",
                        (exc.message, line_id),
                    )
                    conn.execute(
                        "UPDATE batch SET status = 'failed', fail_reason = ? WHERE batch_id = ?",
                        (exc.message, batch_id),
                    )
                failure = {"line_id": line_id, "reason": exc.message, "details": exc.details}
                break
        # 3) 收尾
        if failure is None:
            with self.db.txn() as conn:
                conn.execute(
                    "UPDATE batch SET status = 'issued', issued_at = ? WHERE batch_id = ?",
                    (utcnow(), batch_id),
                )
                batch = self._get_batch_row(conn, batch_id)
                result = {
                    "batch_id": batch_id,
                    "status": "issued",
                    "applied_lines": applied,
                    "manifest_digest": self._manifest(conn, batch)["digest"],
                }
                self._idempotency_finish(conn, request_id, result)
        else:
            result = {
                "batch_id": batch_id,
                "status": "failed",
                "applied_lines": applied,
                "failed_line": failure,
            }
            if request_id is not None:
                with self.db.txn() as conn:
                    self._idempotency_finish(conn, request_id, result)
        return result

    def _apply_line(
        self, conn: sqlite3.Connection, batch_id: str, line_id: str, actor: str
    ) -> None:
        """在事务内应用一条下达行：校验 → 占用 → 台账 → 行状态，全部成功才提交。"""
        line = conn.execute(
            "SELECT * FROM batch_line WHERE line_id = ?", (line_id,)
        ).fetchone()
        if line is None:
            raise NotFoundError("批次行不存在", details={"line_id": line_id})
        if line["status"] == "applied":
            return  # 恢复续办：已入账的行直接跳过，绝不重复扣减
        batch = self._get_batch_row(conn, batch_id)
        pool = self._get_pool_row(conn, line["pool_id"])
        app = self._get_application_row(conn, line["application_id"])
        amount = line["amount"]
        if pool["year"] != batch["year"]:
            raise LineBlockedError(
                "资源池年度与批次年度不一致",
                details={"pool_year": pool["year"], "batch_year": batch["year"]},
            )
        tracks = json.loads(pool["tracks"])
        if line["track"] not in tracks:
            raise LineBlockedError(
                "申报赛道不适用于资源池",
                details={"track": line["track"], "tracks": tracks},
            )
        available = pool["budget_amount"] - pool["locked_amount"]
        if amount > available:
            raise LineBlockedError(
                "可用额度不足",
                details={
                    "pool_id": pool["pool_id"],
                    "available_amount": available,
                    "requested": amount,
                },
            )
        duplicate = conn.execute(
            "SELECT holding_id FROM holding WHERE pool_id = ? AND application_id = ? AND status = 'active'",
            (pool["pool_id"], app["application_id"]),
        ).fetchone()
        if duplicate is not None:
            raise LineBlockedError(
                "同一资源池内该申报已存在在账占用，拒绝重复占用",
                details={"holding_id": duplicate["holding_id"]},
            )
        self._enforce_restrictions(
            conn, pool, applicant=app["applicant"], amount=amount, error=LineBlockedError
        )
        now = utcnow()
        holding_id = self.db.next_id(conn, "HOLD")
        entry_id = self.db.next_id(conn, "LED")
        try:
            conn.execute(
                _INSERT_HOLDING,
                (
                    holding_id,
                    pool["pool_id"],
                    app["application_id"],
                    batch_id,
                    line_id,
                    line["track"],
                    amount,
                    now,
                    now,
                ),
            )
            cursor = conn.execute(
                """UPDATE resource_pool
                   SET locked_amount = locked_amount + ?, version = version + 1
                   WHERE pool_id = ? AND locked_amount + ? <= budget_amount""",
                (amount, pool["pool_id"], amount),
            )
            if cursor.rowcount != 1:
                raise LineBlockedError(
                    "可用额度不足（并发守护）",
                    details={"pool_id": pool["pool_id"], "requested": amount},
                )
            conn.execute(
                _INSERT_LEDGER,
                (
                    entry_id,
                    pool["pool_id"],
                    app["application_id"],
                    holding_id,
                    batch_id,
                    line_id,
                    "lock",
                    amount,
                    pool["locked_amount"] + amount,
                    "正式下达",
                    batch_id,
                    line["issue_key"],
                    actor,
                    now,
                ),
            )
            conn.execute(
                "UPDATE batch_line SET status = 'applied', fail_reason = NULL, applied_at = ? WHERE line_id = ?",
                (now, line_id),
            )
        except sqlite3.IntegrityError:
            # 幂等兜底：台账中已存在同一 issue_key 的入账记录，说明此行已入账
            existing = conn.execute(
                "SELECT entry_id FROM ledger_entry WHERE idempotency_key = ?",
                (line["issue_key"],),
            ).fetchone()
            if existing is not None:
                conn.execute(
                    "UPDATE batch_line SET status = 'applied', fail_reason = NULL, applied_at = ? WHERE line_id = ?",
                    (now, line_id),
                )
                return
            duplicate = conn.execute(
                "SELECT holding_id FROM holding WHERE pool_id = ? AND application_id = ? AND status = 'active'",
                (pool["pool_id"], app["application_id"]),
            ).fetchone()
            if duplicate is not None:
                raise LineBlockedError(
                    "同一资源池内该申报已存在在账占用，拒绝重复占用",
                    details={"holding_id": duplicate["holding_id"]},
                )
            raise
        self._sync_application_status(conn, app["application_id"])

    def adjust_line(
        self,
        *,
        batch_id: str,
        line_id: str,
        new_amount: int,
        reason: str,
        actor: str,
        **_: Any,
    ) -> dict:
        """调减未下达行：留存当时账目快照与理由，失败批次可回到待下达续办。"""
        self._require_int(new_amount, "new_amount", minimum=1)
        reason = self._require_str(reason, "reason")
        actor = self._require_str(actor, "actor")
        with self.db.txn() as conn:
            batch = self._get_batch_row(conn, batch_id)
            if batch["status"] == "issued":
                raise StateError("批次已下达完成，清单不可变；如需变更请走退回或调剂")
            line = conn.execute(
                "SELECT * FROM batch_line WHERE line_id = ? AND batch_id = ?",
                (line_id, batch_id),
            ).fetchone()
            if line is None:
                raise NotFoundError("批次行不存在", details={"line_id": line_id})
            if line["status"] == "applied":
                raise StateError("该行已下达入账，属于不可变清单，不能调减；请走退回或调剂")
            pool = self._get_pool_row(conn, line["pool_id"])
            adjustment_id = self.db.next_id(conn, "ADJ")
            conn.execute(
                """INSERT INTO adjustment
                   (adjustment_id, batch_id, line_id, old_amount, new_amount,
                    pool_available, pool_floor, pool_locked, reason, actor, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    adjustment_id,
                    batch_id,
                    line_id,
                    line["amount"],
                    new_amount,
                    pool["budget_amount"] - pool["locked_amount"],
                    pool["budget_amount"] * pool["floor_ratio_bp"] // 10000,
                    pool["locked_amount"],
                    reason,
                    actor,
                    utcnow(),
                ),
            )
            conn.execute(
                "UPDATE batch_line SET amount = ?, status = 'pending', fail_reason = NULL WHERE line_id = ?",
                (new_amount, line_id),
            )
            if batch["status"] == "failed":
                conn.execute(
                    "UPDATE batch SET status = 'approved', fail_reason = NULL WHERE batch_id = ?",
                    (batch_id,),
                )
            return {
                "adjustment_id": adjustment_id,
                "batch_id": batch_id,
                "line_id": line_id,
                "old_amount": line["amount"],
                "new_amount": new_amount,
            }

    def batch_manifest(self, batch_id: str) -> dict:
        """不可变清单：已入账行 + 台账凭证号 + 内容摘要指纹。"""
        with self.db.txn() as conn:
            batch = self._get_batch_row(conn, batch_id)
            return self._manifest(conn, batch)

    def _manifest(self, conn: sqlite3.Connection, batch: sqlite3.Row) -> dict:
        lines = conn.execute(
            "SELECT * FROM batch_line WHERE batch_id = ? AND status = 'applied' ORDER BY seq",
            (batch["batch_id"],),
        ).fetchall()
        entries = conn.execute(
            "SELECT * FROM ledger_entry WHERE batch_id = ? AND action = 'lock'",
            (batch["batch_id"],),
        ).fetchall()
        by_line = {entry["line_id"]: entry for entry in entries}
        items = []
        for line in lines:
            entry = by_line.get(line["line_id"])
            items.append(
                {
                    "line_id": line["line_id"],
                    "seq": line["seq"],
                    "pool_id": line["pool_id"],
                    "application_id": line["application_id"],
                    "track": line["track"],
                    "amount": line["amount"],
                    "holding_id": entry["holding_id"] if entry else None,
                    "ledger_entry_id": entry["entry_id"] if entry else None,
                    "applied_at": line["applied_at"],
                }
            )
        payload = {
            "batch_id": batch["batch_id"],
            "plan_id": batch["plan_id"],
            "year": batch["year"],
            "status": batch["status"],
            "issued_at": batch["issued_at"],
            "lines": items,
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {
            **payload,
            "complete": batch["status"] == "issued",
            "digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        }

    # ------------------------------------------------------------------
    # 退回与部分释放
    # ------------------------------------------------------------------
    def return_application(
        self,
        *,
        application_id: str,
        reason: str,
        actor: str,
        releases: list | None = None,
        request_id: str | None = None,
        **_: Any,
    ) -> dict:
        """退回申报：默认释放全部在账占用；也可按 releases 指定部分释放。"""
        reason = self._require_str(reason, "reason")
        actor = self._require_str(actor, "actor")
        with self.db.txn() as conn:
            cached = self._idempotency_begin(conn, request_id, "return_application")
            if cached is not None:
                return cached
            self._get_application_row(conn, application_id)
            holdings = conn.execute(
                "SELECT * FROM holding WHERE application_id = ? AND status = 'active' ORDER BY created_at",
                (application_id,),
            ).fetchall()
            if not holdings:
                raise StateError("该申报没有在账占用，无需退回")
            targets = self._resolve_releases(holdings, releases)
            released = []
            for holding, amount in targets:
                self._release_locked(
                    conn,
                    holding=holding,
                    amount=amount,
                    reason=reason,
                    actor=actor,
                    group_id=f"RETURN:{application_id}",
                )
                released.append(
                    {
                        "holding_id": holding["holding_id"],
                        "pool_id": holding["pool_id"],
                        "amount": amount,
                    }
                )
            self._sync_application_status(conn, application_id)
            status = conn.execute(
                "SELECT status FROM application WHERE application_id = ?", (application_id,)
            ).fetchone()["status"]
            result = {"application_id": application_id, "status": status, "released": released}
            self._idempotency_finish(conn, request_id, result)
            return result

    def release_holding(
        self,
        *,
        holding_id: str,
        amount: int,
        reason: str,
        actor: str,
        request_id: str | None = None,
        **_: Any,
    ) -> dict:
        """部分释放：只释放某笔占用的一部分，账实同步扣减。"""
        self._require_int(amount, "amount", minimum=1)
        reason = self._require_str(reason, "reason")
        actor = self._require_str(actor, "actor")
        with self.db.txn() as conn:
            cached = self._idempotency_begin(conn, request_id, "release_holding")
            if cached is not None:
                return cached
            holding = conn.execute(
                "SELECT * FROM holding WHERE holding_id = ?", (holding_id,)
            ).fetchone()
            if holding is None:
                raise NotFoundError("占用记录不存在", details={"holding_id": holding_id})
            if holding["status"] != "active":
                raise StateError("该占用已释放，不能重复释放")
            if amount > holding["amount"]:
                raise ValidationError(
                    "释放数量超过在账数量",
                    details={"holding_amount": holding["amount"], "requested": amount},
                )
            self._release_locked(
                conn,
                holding=holding,
                amount=amount,
                reason=reason,
                actor=actor,
                group_id=f"RELEASE:{holding_id}",
            )
            self._sync_application_status(conn, holding["application_id"])
            remaining = holding["amount"] - amount
            result = {
                "holding_id": holding_id,
                "pool_id": holding["pool_id"],
                "released_amount": amount,
                "remaining_amount": remaining,
                "holding_status": "released" if remaining == 0 else "active",
            }
            self._idempotency_finish(conn, request_id, result)
            return result

    @staticmethod
    def _resolve_releases(holdings: list, releases: list | None) -> list:
        by_id = {holding["holding_id"]: holding for holding in holdings}
        if releases is None:
            return [(holding, holding["amount"]) for holding in holdings]
        if not isinstance(releases, list) or not releases:
            raise ValidationError("releases 必须是非空数组")
        targets = []
        seen = set()
        for item in releases:
            if not isinstance(item, dict):
                raise ValidationError("releases 条目必须是对象")
            holding_id = item.get("holding_id")
            if holding_id not in by_id:
                raise ValidationError(
                    "释放目标不属于该申报的在账占用",
                    details={"holding_id": holding_id},
                )
            if holding_id in seen:
                raise ValidationError(
                    "释放目标重复", details={"holding_id": holding_id}
                )
            seen.add(holding_id)
            amount = AllocationService._require_int(item.get("amount"), "amount", minimum=1)
            if amount > by_id[holding_id]["amount"]:
                raise ValidationError(
                    "释放数量超过在账数量",
                    details={
                        "holding_id": holding_id,
                        "holding_amount": by_id[holding_id]["amount"],
                        "requested": amount,
                    },
                )
            targets.append((by_id[holding_id], amount))
        return targets

    def _release_locked(
        self,
        conn: sqlite3.Connection,
        *,
        holding: sqlite3.Row,
        amount: int,
        reason: str,
        actor: str,
        group_id: str,
    ) -> str:
        """在事务内释放一笔占用：占用扣减 + 池锁定扣减 + 台账流水。"""
        pool = self._get_pool_row(conn, holding["pool_id"])
        remaining = holding["amount"] - amount
        if remaining < 0:
            raise ValidationError("释放数量超过在账数量")
        now = utcnow()
        conn.execute(
            "UPDATE holding SET amount = ?, status = ?, updated_at = ? WHERE holding_id = ?",
            (remaining, "released" if remaining == 0 else "active", now, holding["holding_id"]),
        )
        cursor = conn.execute(
            """UPDATE resource_pool
               SET locked_amount = locked_amount - ?, version = version + 1
               WHERE pool_id = ? AND locked_amount - ? >= 0""",
            (amount, pool["pool_id"], amount),
        )
        if cursor.rowcount != 1:
            raise StateError("账实异常：锁定余额不足，无法释放")
        entry_id = self.db.next_id(conn, "LED")
        conn.execute(
            _INSERT_LEDGER,
            (
                entry_id,
                pool["pool_id"],
                holding["application_id"],
                holding["holding_id"],
                holding["batch_id"],
                None,
                "release",
                amount,
                pool["locked_amount"] - amount,
                reason,
                group_id,
                entry_id,
                actor,
                now,
            ),
        )
        return entry_id

    # ------------------------------------------------------------------
    # 跨项目调剂
    # ------------------------------------------------------------------
    def reallocate(
        self,
        *,
        from_holding_id: str,
        to_application_id: str,
        amount: int,
        reason: str,
        actor: str,
        to_pool_id: str | None = None,
        request_id: str | None = None,
        **_: Any,
    ) -> dict:
        """跨项目调剂：把在账占用从一个项目调到另一个项目（可跨同类型资源池）。

        保底约束：跨池调出后源池锁定不得低于保底额度，报错中带可解释依据。
        """
        self._require_int(amount, "amount", minimum=1)
        reason = self._require_str(reason, "reason")
        actor = self._require_str(actor, "actor")
        with self.db.txn() as conn:
            cached = self._idempotency_begin(conn, request_id, "reallocate")
            if cached is not None:
                return cached
            src = conn.execute(
                "SELECT * FROM holding WHERE holding_id = ?", (from_holding_id,)
            ).fetchone()
            if src is None:
                raise NotFoundError("源占用不存在", details={"holding_id": from_holding_id})
            if src["status"] != "active":
                raise StateError("源占用已释放，不能调出")
            if amount > src["amount"]:
                raise ValidationError(
                    "调剂数量超过源占用在账数量",
                    details={"holding_amount": src["amount"], "requested": amount},
                )
            src_pool = self._get_pool_row(conn, src["pool_id"])
            dst_pool = self._get_pool_row(conn, to_pool_id) if to_pool_id else src_pool
            dst_app = self._get_application_row(conn, to_application_id)
            if (
                dst_pool["pool_id"] == src_pool["pool_id"]
                and dst_app["application_id"] == src["application_id"]
            ):
                raise ValidationError("调入项目与调出项目相同，无需调剂")
            cross_pool = dst_pool["pool_id"] != src_pool["pool_id"]
            if cross_pool:
                if dst_pool["year"] != src_pool["year"]:
                    raise ValidationError(
                        "跨池调剂要求同一年度",
                        details={"from_year": src_pool["year"], "to_year": dst_pool["year"]},
                    )
                if (
                    dst_pool["resource_type"] != src_pool["resource_type"]
                    or dst_pool["unit"] != src_pool["unit"]
                ):
                    raise ValidationError("跨池调剂要求同一资源类型与计量单位")
                floor_amount = src_pool["budget_amount"] * src_pool["floor_ratio_bp"] // 10000
                if src_pool["locked_amount"] - amount < floor_amount:
                    raise QuotaError(
                        "调出后将跌破保底额度",
                        details={
                            "pool_id": src_pool["pool_id"],
                            "floor_amount": floor_amount,
                            "locked_amount": src_pool["locked_amount"],
                            "max_transferable": max(0, src_pool["locked_amount"] - floor_amount),
                            "requested": amount,
                        },
                    )
                available = dst_pool["budget_amount"] - dst_pool["locked_amount"]
                if amount > available:
                    raise QuotaError(
                        "调入池可用额度不足",
                        details={
                            "pool_id": dst_pool["pool_id"],
                            "available_amount": available,
                            "requested": amount,
                        },
                    )
            tracks = json.loads(dst_pool["tracks"])
            if dst_app["track"] not in tracks:
                raise QuotaError(
                    "调入项目赛道不适用于目标资源池",
                    details={"track": dst_app["track"], "tracks": tracks},
                )
            self._enforce_restrictions(
                conn, dst_pool, applicant=dst_app["applicant"], amount=amount
            )
            realloc_id = self.db.next_id(conn, "REALLOC")
            now = utcnow()
            # 调出：源占用扣减
            src_remaining = src["amount"] - amount
            conn.execute(
                "UPDATE holding SET amount = ?, status = ?, updated_at = ? WHERE holding_id = ?",
                (
                    src_remaining,
                    "released" if src_remaining == 0 else "active",
                    now,
                    src["holding_id"],
                ),
            )
            if cross_pool:
                cursor = conn.execute(
                    """UPDATE resource_pool
                       SET locked_amount = locked_amount - ?, version = version + 1
                       WHERE pool_id = ? AND locked_amount - ? >= 0""",
                    (amount, src_pool["pool_id"], amount),
                )
                if cursor.rowcount != 1:
                    raise StateError("账实异常：源池锁定余额不足")
            # 调入：合并或新建目标占用
            dst = conn.execute(
                "SELECT * FROM holding WHERE pool_id = ? AND application_id = ? AND status = 'active'",
                (dst_pool["pool_id"], dst_app["application_id"]),
            ).fetchone()
            if dst is not None:
                conn.execute(
                    "UPDATE holding SET amount = amount + ?, updated_at = ? WHERE holding_id = ?",
                    (amount, now, dst["holding_id"]),
                )
                dst_holding_id = dst["holding_id"]
            else:
                dst_holding_id = self.db.next_id(conn, "HOLD")
                conn.execute(
                    _INSERT_HOLDING,
                    (
                        dst_holding_id,
                        dst_pool["pool_id"],
                        dst_app["application_id"],
                        f"REALLOC:{realloc_id}",
                        f"REALLOC:{realloc_id}",
                        dst_app["track"],
                        amount,
                        now,
                        now,
                    ),
                )
            if cross_pool:
                cursor = conn.execute(
                    """UPDATE resource_pool
                       SET locked_amount = locked_amount + ?, version = version + 1
                       WHERE pool_id = ? AND locked_amount + ? <= budget_amount""",
                    (amount, dst_pool["pool_id"], amount),
                )
                if cursor.rowcount != 1:
                    raise QuotaError("调入池可用额度不足（并发守护）")
            # 台账：两条不可变流水，共享调剂号以便对照
            out_entry = self.db.next_id(conn, "LED")
            src_locked_after = (
                src_pool["locked_amount"] - amount if cross_pool else src_pool["locked_amount"]
            )
            conn.execute(
                _INSERT_LEDGER,
                (
                    out_entry,
                    src_pool["pool_id"],
                    src["application_id"],
                    src["holding_id"],
                    None,
                    None,
                    "transfer_out",
                    amount,
                    src_locked_after,
                    reason,
                    realloc_id,
                    f"{realloc_id}:out",
                    actor,
                    now,
                ),
            )
            in_entry = self.db.next_id(conn, "LED")
            dst_locked_after = (
                dst_pool["locked_amount"] + amount if cross_pool else dst_pool["locked_amount"]
            )
            conn.execute(
                _INSERT_LEDGER,
                (
                    in_entry,
                    dst_pool["pool_id"],
                    dst_app["application_id"],
                    dst_holding_id,
                    None,
                    None,
                    "transfer_in",
                    amount,
                    dst_locked_after,
                    reason,
                    realloc_id,
                    f"{realloc_id}:in",
                    actor,
                    now,
                ),
            )
            self._sync_application_status(conn, src["application_id"])
            self._sync_application_status(conn, dst_app["application_id"])
            result = {
                "reallocation_id": realloc_id,
                "amount": amount,
                "from": {
                    "pool_id": src_pool["pool_id"],
                    "application_id": src["application_id"],
                    "holding_id": src["holding_id"],
                    "remaining_amount": src_remaining,
                },
                "to": {
                    "pool_id": dst_pool["pool_id"],
                    "application_id": dst_app["application_id"],
                    "holding_id": dst_holding_id,
                },
                "ledger_entries": [out_entry, in_entry],
            }
            self._idempotency_finish(conn, request_id, result)
            return result

    # ------------------------------------------------------------------
    # 限制条件与状态同步
    # ------------------------------------------------------------------
    def _enforce_restrictions(
        self,
        conn: sqlite3.Connection,
        pool: sqlite3.Row,
        *,
        applicant: str,
        amount: int,
        error: type[QuotaError] = QuotaError,
    ) -> None:
        rules = json.loads(pool["restrictions"])
        cap = rules.get(MAX_SINGLE_AMOUNT)
        if cap is not None and amount > cap:
            raise error(
                "超过该资源池单项上限",
                details={"max_single_amount": cap, "requested": amount},
            )
        allowed = rules.get(ALLOWED_APPLICANTS)
        if allowed is not None and applicant not in allowed:
            raise error(
                "该高校不在资源池允许名单内",
                details={"applicant": applicant, "allowed_applicants": allowed},
            )
        per_cap = rules.get(PER_APPLICANT_CAP)
        if per_cap is not None:
            row = conn.execute(
                """SELECT COALESCE(SUM(h.amount), 0) AS total
                   FROM holding h JOIN application a ON a.application_id = h.application_id
                   WHERE h.pool_id = ? AND a.applicant = ? AND h.status = 'active'""",
                (pool["pool_id"], applicant),
            ).fetchone()
            if row["total"] + amount > per_cap:
                raise error(
                    "超过该高校在资源池的在账上限",
                    details={
                        "per_applicant_cap": per_cap,
                        "current": row["total"],
                        "requested": amount,
                    },
                )

    def _restriction_violations(
        self,
        conn: sqlite3.Connection,
        pool: sqlite3.Row,
        *,
        applicant: str,
        amount: int,
        planned_total: int,
    ) -> list:
        """试算专用的非阻断检查：返回违反的限制条件说明列表。"""
        rules = json.loads(pool["restrictions"])
        violations = []
        cap = rules.get(MAX_SINGLE_AMOUNT)
        if cap is not None and amount > cap:
            violations.append(f"超过单项上限 {cap}")
        allowed = rules.get(ALLOWED_APPLICANTS)
        if allowed is not None and applicant not in allowed:
            violations.append("高校不在允许名单内")
        per_cap = rules.get(PER_APPLICANT_CAP)
        if per_cap is not None:
            row = conn.execute(
                """SELECT COALESCE(SUM(h.amount), 0) AS total
                   FROM holding h JOIN application a ON a.application_id = h.application_id
                   WHERE h.pool_id = ? AND a.applicant = ? AND h.status = 'active'""",
                (pool["pool_id"], applicant),
            ).fetchone()
            if row["total"] + planned_total > per_cap:
                violations.append(
                    f"超过高校在账上限 {per_cap}（当前在账 {row['total']}，方案合计 {planned_total}）"
                )
        return violations

    def _sync_application_status(self, conn: sqlite3.Connection, application_id: str) -> None:
        """申报状态由在账占用与台账流水确定性推导。"""
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM holding WHERE application_id = ? GROUP BY status",
            (application_id,),
        ).fetchall()
        counts = {row["status"]: row["c"] for row in rows}
        active = counts.get("active", 0)
        released = counts.get("released", 0)
        if active == 0 and released == 0:
            return  # 从未下达
        if active == 0:
            status = "returned"
        else:
            # 仍有在账占用：只要台账中出现过释放/调出流水，即为部分退回
            has_release = conn.execute(
                """SELECT 1 FROM ledger_entry
                   WHERE application_id = ? AND action IN ('release', 'transfer_out') LIMIT 1""",
                (application_id,),
            ).fetchone()
            status = "partially_returned" if has_release else "issued"
        conn.execute(
            "UPDATE application SET status = ? WHERE application_id = ?",
            (status, application_id),
        )

    # ------------------------------------------------------------------
    # 对账与恢复
    # ------------------------------------------------------------------
    def reconcile(self, pool_id: str | None = None) -> dict:
        """账实核对：池锁定余额 = 在账占用之和 = 台账有符号流水之和。"""
        with self.db.txn() as conn:
            if pool_id is not None:
                pools = [self._get_pool_row(conn, pool_id)]
            else:
                pools = conn.execute("SELECT * FROM resource_pool ORDER BY pool_id").fetchall()
            reports = []
            for pool in pools:
                holdings_sum = conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) AS s FROM holding WHERE pool_id = ? AND status = 'active'",
                    (pool["pool_id"],),
                ).fetchone()["s"]
                ledger_sum = conn.execute(
                    """SELECT COALESCE(SUM(CASE action
                           WHEN 'lock' THEN amount
                           WHEN 'transfer_in' THEN amount
                           WHEN 'release' THEN -amount
                           WHEN 'transfer_out' THEN -amount END), 0) AS s
                       FROM ledger_entry WHERE pool_id = ?""",
                    (pool["pool_id"],),
                ).fetchone()["s"]
                last = conn.execute(
                    "SELECT locked_after FROM ledger_entry WHERE pool_id = ? ORDER BY rowid DESC LIMIT 1",
                    (pool["pool_id"],),
                ).fetchone()
                stored = pool["locked_amount"]
                consistent = (
                    stored == holdings_sum == ledger_sum
                    and (last is None or last["locked_after"] == stored)
                )
                reports.append(
                    {
                        "pool_id": pool["pool_id"],
                        "stored_locked": stored,
                        "holdings_locked": holdings_sum,
                        "ledger_locked": ledger_sum,
                        "consistent": consistent,
                    }
                )
            return {
                "consistent": all(report["consistent"] for report in reports),
                "pools": reports,
            }

    def recover(self, actor: str = "system") -> dict:
        """恢复续办：继续处理所有"下达中"的批次，已入账行自动跳过。"""
        rows = self.db.conn.execute(
            "SELECT batch_id FROM batch WHERE status = 'issuing' ORDER BY created_at"
        ).fetchall()
        resumed = []
        for row in rows:
            result = self.issue_batch(batch_id=row["batch_id"], actor=actor)
            resumed.append({"batch_id": row["batch_id"], "status": result["status"]})
        return {"resumed_count": len(resumed), "batches": resumed}
