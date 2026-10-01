"""领域服务：建账、试算、审批、下达、退回、部分释放与跨项目调剂。

并发与一致性策略
----------------
1. 所有额度变动在 ``BEGIN IMMEDIATE`` 事务内完成，SQLite 写锁串行化，
   杜绝两个方案同时看到相同可用额度而重复占用。
2. 批次（下达/退回/释放）与调剂单先持久化"意图清单"，再执行落账：
   服务崩溃后 :meth:`ResourceService.recover` 可继续未完成批次；
   执行事务要么整体提交、要么整体回滚，恢复重试不会重复扣减。
3. 每个写操作要求幂等键，重复请求返回首次结果。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import ledger
from .errors import (
    ConflictError,
    ConstraintViolationError,
    DomainError,
    IdempotencyConflictError,
    InvalidStateError,
    LedgerIntegrityError,
    NotFoundError,
    QuotaExceededError,
    ValidationError,
)
from .ledger import utcnow
from .models import BatchStatus, PlanStatus, TransferStatus, TxnType

BPS = 10_000
_RUNNABLE_BATCH = (BatchStatus.PENDING.value, BatchStatus.RUNNING.value)
# 下达之后允许继续释放/调剂的状态
_LIVE_STATES = (
    PlanStatus.ISSUED.value,
    PlanStatus.PARTIALLY_RELEASED.value,
)


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


class ResourceService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------ 建账

    def create_account(
        self,
        *,
        year: int,
        kind: str,
        name: str,
        total_amount: int,
        tracks: list[dict[str, Any]],
        max_per_project: int | None = None,
    ) -> dict[str, Any]:
        """按 年度+资源类型 建账，登记适用赛道与保底比例（基点）。"""
        if not isinstance(year, int) or year < 2000 or year > 2200:
            raise ValidationError("年度不合法", year=year)
        if not isinstance(total_amount, int) or total_amount <= 0:
            raise ValidationError("总额度必须是正整数（资金单位：分）")
        if max_per_project is not None and (
            not isinstance(max_per_project, int) or max_per_project <= 0
        ):
            raise ValidationError("每项目上限必须是正整数或为空")
        if not tracks:
            raise ValidationError("至少登记一个适用赛道")
        norm: dict[str, int] = {}
        for t in tracks:
            track = str(t.get("track", "")).strip()
            bps = t.get("guarantee_bps", 0)
            if not track:
                raise ValidationError("赛道名称不能为空")
            if not isinstance(bps, int) or not 0 <= bps <= BPS:
                raise ValidationError("保底比例必须是 0..10000 的基点整数", track=track)
            if track in norm:
                raise ValidationError("赛道重复登记", track=track)
            norm[track] = bps
        if sum(norm.values()) > BPS:
            raise ValidationError("各赛道保底比例之和不能超过 100%")

        ledger.begin_immediate(self.conn)
        try:
            exists = self.conn.execute(
                "SELECT 1 FROM accounts WHERE year=? AND kind=? AND name=?",
                (year, kind, name),
            ).fetchone()
            if exists:
                raise ConflictError("同年度同类型同名账户已存在", year=year, kind=kind, name=name)
            cur = self.conn.execute(
                "INSERT INTO accounts(year, kind, name, total_amount, "
                "max_per_project, created_at) VALUES (?,?,?,?,?,?)",
                (year, kind, name, total_amount, max_per_project, utcnow()),
            )
            account_id = int(cur.lastrowid)
            self.conn.executemany(
                "INSERT INTO account_tracks(account_id, track, guarantee_bps) "
                "VALUES (?,?,?)",
                [(account_id, t, b) for t, b in norm.items()],
            )
            ledger.commit(self.conn)
        except Exception:
            ledger.rollback(self.conn)
            raise
        return self.get_account(account_id)

    def get_account(self, account_id: int) -> dict[str, Any]:
        acct = self.conn.execute(
            "SELECT * FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
        if acct is None:
            raise NotFoundError("账户不存在", account_id=account_id)
        out = _row(acct)
        out["tracks"] = [
            dict(r)
            for r in self.conn.execute(
                "SELECT track, guarantee_bps FROM account_tracks "
                "WHERE account_id=? ORDER BY track",
                (account_id,),
            )
        ]
        return out

    def list_accounts(self, year: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id FROM accounts"
        params: tuple[Any, ...] = ()
        if year is not None:
            sql += " WHERE year=?"
            params = (year,)
        sql += " ORDER BY year, kind, id"
        return [self.get_account(r["id"]) for r in self.conn.execute(sql, params)]

    def account_snapshot(self, account_id: int) -> dict[str, Any]:
        snap = ledger.account_snapshot(self.conn, account_id)
        tracks = {
            r["track"]: r["guarantee_bps"]
            for r in self.conn.execute(
                "SELECT track, guarantee_bps FROM account_tracks WHERE account_id=?",
                (account_id,),
            )
        }
        snap["guarantees_bps"] = tracks
        return snap

    # ------------------------------------------------------------------ 项目

    def create_project(self, *, code: str, name: str, track: str) -> dict[str, Any]:
        code = str(code).strip()
        name = str(name).strip()
        track = str(track).strip()
        if not code or not name or not track:
            raise ValidationError("项目编号、名称、赛道均不能为空")
        ledger.begin_immediate(self.conn)
        try:
            if self.conn.execute(
                "SELECT 1 FROM projects WHERE code=?", (code,)
            ).fetchone():
                raise ConflictError("项目编号已存在", code=code)
            cur = self.conn.execute(
                "INSERT INTO projects(code, name, track, created_at) "
                "VALUES (?,?,?,?)",
                (code, name, track, utcnow()),
            )
            ledger.commit(self.conn)
        except Exception:
            ledger.rollback(self.conn)
            raise
        return self.get_project(int(cur.lastrowid))

    def get_project(self, project_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("项目不存在", project_id=project_id)
        return dict(row)

    def _project_by_code(self, code: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM projects WHERE code=?", (code,)
        ).fetchone()
        if row is None:
            raise NotFoundError("项目不存在", code=code)
        return row

    # ------------------------------------------------------------ 试算方案

    def create_plan(
        self,
        *,
        project_code: str,
        year: int,
        title: str,
        items: list[dict[str, Any]] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """创建试算方案（草案）。草案完全不进台账，彼此隔离。"""
        project = self._project_by_code(project_code)
        if not title or not str(title).strip():
            raise ValidationError("方案标题不能为空")
        items = items or []
        norm_items = self._normalize_items(items)

        ledger.begin_immediate(self.conn)
        try:
            if idempotency_key:
                old = self.conn.execute(
                    "SELECT * FROM plans WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if old is not None:
                    if old["project_id"] != project["id"] or old["title"] != title:
                        raise IdempotencyConflictError(
                            "幂等键已用于其他方案", idempotency_key=idempotency_key
                        )
                    ledger.commit(self.conn)
                    return self.get_plan(old["id"])
            now = utcnow()
            cur = self.conn.execute(
                "INSERT INTO plans(project_id, year, title, status, "
                "idempotency_key, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    project["id"], year, title, PlanStatus.DRAFT.value,
                    idempotency_key, now, now,
                ),
            )
            plan_id = int(cur.lastrowid)
            self._write_items(plan_id, norm_items)
            ledger.commit(self.conn)
        except Exception:
            ledger.rollback(self.conn)
            raise
        return self.get_plan(plan_id)

    @staticmethod
    def _normalize_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(items, list):
            raise ValidationError("明细必须是列表")
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for it in items:
            kind = str(it.get("kind", "")).strip()
            amount = it.get("amount")
            if kind not in ("fund", "lab", "faculty"):
                raise ValidationError("资源类型必须是 fund/lab/faculty", kind=kind)
            if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
                raise ValidationError("数量必须是正整数（资金单位：分）", kind=kind)
            if kind in seen:
                raise ValidationError("同一方案中资源类型不能重复", kind=kind)
            seen.add(kind)
            out.append(
                {
                    "kind": kind,
                    "amount": amount,
                    "note": str(it.get("note", "")),
                    "account": str(it.get("account", "")).strip() or None,
                }
            )
        return out

    def _write_items(self, plan_id: int, items: list[dict[str, Any]]) -> None:
        self.conn.execute("DELETE FROM plan_items WHERE plan_id=?", (plan_id,))
        self.conn.executemany(
            "INSERT INTO plan_items(plan_id, kind, amount, account_name, note) "
            "VALUES (?,?,?,?,?)",
            [
                (plan_id, it["kind"], it["amount"], it["account"], it["note"])
                for it in items
            ],
        )

    def get_plan(self, plan_id: int) -> dict[str, Any]:
        plan = self.conn.execute(
            "SELECT * FROM plans WHERE id=?", (plan_id,)
        ).fetchone()
        if plan is None:
            raise NotFoundError("方案不存在", plan_id=plan_id)
        out = dict(plan)
        out["items"] = [
            dict(r)
            for r in self.conn.execute(
                "SELECT kind, amount, account_name, note FROM plan_items "
                "WHERE plan_id=? ORDER BY kind",
                (plan_id,),
            )
        ]
        out["project"] = self.get_project(plan["project_id"])
        return out

    def list_plans(
        self, *, year: int | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT id FROM plans WHERE 1=1"
        params: list[Any] = []
        if year is not None:
            sql += " AND year=?"
            params.append(year)
        if status is not None:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY id"
        return [self.get_plan(r["id"]) for r in self.conn.execute(sql, params)]

    def update_plan_items(self, plan_id: int, items: list[dict[str, Any]]) -> dict[str, Any]:
        plan = self._require_plan(plan_id)
        if plan["status"] != PlanStatus.DRAFT.value:
            raise InvalidStateError(
                "只有草案可以修改明细", plan_id=plan_id, status=plan["status"]
            )
        norm = self._normalize_items(items)
        ledger.begin_immediate(self.conn)
        try:
            self._write_items(plan_id, norm)
            self._touch(plan_id)
            ledger.commit(self.conn)
        except Exception:
            ledger.rollback(self.conn)
            raise
        return self.get_plan(plan_id)

    def delete_plan(self, plan_id: int) -> None:
        plan = self._require_plan(plan_id)
        if plan["status"] != PlanStatus.DRAFT.value:
            raise InvalidStateError("只有草案可以删除", plan_id=plan_id)
        ledger.begin_immediate(self.conn)
        try:
            self.conn.execute("DELETE FROM plan_items WHERE plan_id=?", (plan_id,))
            self.conn.execute("DELETE FROM plans WHERE id=?", (plan_id,))
            ledger.commit(self.conn)
        except Exception:
            ledger.rollback(self.conn)
            raise

    def submit_plan(self, plan_id: int) -> dict[str, Any]:
        return self._transition(
            plan_id,
            allowed={PlanStatus.DRAFT.value},
            to=PlanStatus.SUBMITTED.value,
        )

    def approve_plan(self, plan_id: int) -> dict[str, Any]:
        return self._transition(
            plan_id,
            allowed={PlanStatus.SUBMITTED.value, PlanStatus.REJECTED.value},
            to=PlanStatus.APPROVED.value,
        )

    def reject_plan(self, plan_id: int, *, reason: str, idem_key: str) -> dict[str, Any]:
        """退回申请并记录依据。

        - 尚未下达（已提交/已批准）：不占额度，仅状态扭转；
        - 已下达：全部未结清额以 reject_release 分录退回，额度回收、方案结清。
        """
        # 幂等回放最先处理：方案后续状态变化不应影响首次结果的重放
        replay = self._idem_batch(idem_key, "reject", plan_id)
        if replay is not None:
            return replay
        plan = self._require_plan(plan_id)
        if not str(reason).strip():
            raise ValidationError("退回必须填写可解释的依据（reason）")
        if plan["status"] in (PlanStatus.SUBMITTED.value, PlanStatus.APPROVED.value):
            return self._register_and_run_batch(
                plan_id=plan_id,
                idem_key=idem_key,
                action="reject",
                reason=reason,
                manifest=[],
                execute=lambda batch_id: self._execute_reject(batch_id, plan_id, reason),
            )
        if plan["status"] in _LIVE_STATES:
            allocs = self.conn.execute(
                "SELECT id FROM allocations WHERE plan_id=?", (plan_id,)
            ).fetchall()
            manifest: list[dict[str, Any]] = []
            for r in allocs:
                outstanding = ledger.allocation_balance(self.conn, r["id"])
                if outstanding > 0:
                    manifest.append(
                        {"allocation_id": r["id"], "amount": outstanding, "mode": "reject"}
                    )
            if not manifest:
                raise InvalidStateError(
                    "方案已无未结清资源可退回", plan_id=plan_id, status=plan["status"]
                )
            return self._register_and_run_batch(
                plan_id=plan_id,
                idem_key=idem_key,
                action="reject",
                reason=reason,
                manifest=manifest,
                execute=lambda batch_id: self._execute_post_issue_reject(
                    batch_id, plan_id, reason
                ),
            )
        raise InvalidStateError(
            "当前状态不能退回", plan_id=plan_id, status=plan["status"]
        )

    def _transition(
        self, plan_id: int, *, allowed: set[str], to: str
    ) -> dict[str, Any]:
        plan = self._require_plan(plan_id)
        if plan["status"] not in allowed:
            raise InvalidStateError(
                "方案状态不允许此操作",
                plan_id=plan_id,
                status=plan["status"],
                required=sorted(allowed),
            )
        ledger.begin_immediate(self.conn)
        try:
            self.conn.execute(
                "UPDATE plans SET status=?, updated_at=? WHERE id=?",
                (to, utcnow(), plan_id),
            )
            ledger.commit(self.conn)
        except Exception:
            ledger.rollback(self.conn)
            raise
        return self.get_plan(plan_id)

    def _touch(self, plan_id: int) -> None:
        self.conn.execute(
            "UPDATE plans SET updated_at=? WHERE id=?", (utcnow(), plan_id)
        )

    def _require_plan(self, plan_id: int) -> sqlite3.Row:
        plan = self.conn.execute(
            "SELECT * FROM plans WHERE id=?", (plan_id,)
        ).fetchone()
        if plan is None:
            raise NotFoundError("方案不存在", plan_id=plan_id)
        return plan

    # ------------------------------------------------------------ 试算预览

    def _resolve_account(
        self, year: int, kind: str, account_name: str | None
    ) -> sqlite3.Row:
        if account_name:
            row = self.conn.execute(
                "SELECT * FROM accounts WHERE year=? AND kind=? AND name=?",
                (year, kind, account_name),
            ).fetchone()
            if row is None:
                raise NotFoundError(
                    "账户不存在", year=year, kind=kind, account=account_name
                )
            return row
        rows = self.conn.execute(
            "SELECT * FROM accounts WHERE year=? AND kind=?", (year, kind)
        ).fetchall()
        if not rows:
            raise NotFoundError("该年度/类型尚未建账", year=year, kind=kind)
        if len(rows) > 1:
            raise ValidationError(
                "该年度/类型存在多个账户，明细须指定 account 名称",
                choices=[r["name"] for r in rows],
            )
        return rows[0]

    def _account_tracks(self, account_id: int) -> dict[str, int]:
        return {
            r["track"]: r["guarantee_bps"]
            for r in self.conn.execute(
                "SELECT track, guarantee_bps FROM account_tracks WHERE account_id=?",
                (account_id,),
            )
        }

    def _floor_feasible(
        self,
        account: sqlite3.Row,
        tracks: dict[str, int],
        delta_by_track: dict[str, int],
    ) -> tuple[bool, dict[str, Any]]:
        """检查发生赛道净变化（可正可负）后，各赛道保底比例仍可实现。

        条件：落账后占用不超总额度，且剩余可用额度 >= 所有未达保底赛道的缺口之和。
        """
        usage = ledger.usage_by_track(self.conn, account["id"])
        for t, v in delta_by_track.items():
            usage[t] = usage.get(t, 0) + v
        total = account["total_amount"]
        used = sum(usage.values())
        available = total - used
        floors = {t: tracks[t] for t in tracks if tracks[t] > 0}
        gap = sum(
            max(0, (bps * total + BPS - 1) // BPS - usage.get(t, 0))
            for t, bps in floors.items()
        )
        detail = {
            "total": total,
            "used_after": used,
            "available_after": available,
            "guarantee_gap": gap,
            "usage_by_track_after": usage,
        }
        if used > total:
            detail["reason"] = "年度额度不足"
            return False, detail
        if available < gap:
            detail["reason"] = "将挤占其他赛道的保底额度"
            return False, detail
        return True, detail

    def preview_plan(self, plan_id: int) -> dict[str, Any]:
        """纯读试算：校验适用赛道、每项目上限、年度额度与保底可行性。

        不落任何数据，多个方案可同时试算、互不影响。
        """
        plan = self._require_plan(plan_id)
        project = self.get_project(plan["project_id"])
        items = self.conn.execute(
            "SELECT * FROM plan_items WHERE plan_id=? ORDER BY kind", (plan_id,)
        ).fetchall()
        if not items:
            raise ValidationError("方案没有明细，无法试算")
        checks: list[dict[str, Any]] = []
        per_account_add: dict[int, dict[str, int]] = {}
        ok = True
        for it in items:
            account = self._resolve_account(
                plan["year"], it["kind"], it["account_name"]
            )
            tracks = self._account_tracks(account["id"])
            item_check: dict[str, Any] = {
                "kind": it["kind"],
                "amount": it["amount"],
                "account_id": account["id"],
                "account_name": account["name"],
            }
            problems: list[str] = []
            if project["track"] not in tracks:
                problems.append("项目赛道不在账户适用范围内")
            current = ledger.project_used(
                self.conn, project["id"], account["id"]
            )
            if (
                account["max_per_project"] is not None
                and current + it["amount"] > account["max_per_project"]
            ):
                problems.append("超过每项目上限")
                item_check["max_per_project"] = account["max_per_project"]
            item_check["project_used_now"] = current
            item_check["passed"] = not problems
            item_check["problems"] = problems
            checks.append(item_check)
            per_account_add.setdefault(account["id"], {})
            d = per_account_add[account["id"]]
            d[project["track"]] = d.get(project["track"], 0) + it["amount"]
            ok = ok and not problems

        for account_id, additional in per_account_add.items():
            account = self.conn.execute(
                "SELECT * FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
            tracks = self._account_tracks(account_id)
            feasible, detail = self._floor_feasible(account, tracks, additional)
            if not feasible:
                ok = False
            checks.append(
                {
                    "account_id": account_id,
                    "account_name": account["name"],
                    "kind": account["kind"],
                    "passed": feasible,
                    "problems": [] if feasible else [detail["reason"]],
                    "capacity": detail,
                }
            )
        # 提示：同年度已批准待下达的其他方案也在排队，可能争先占用
        pending = [
            self.get_plan(r["id"])
            for r in self.conn.execute(
                "SELECT id FROM plans WHERE year=? AND status=? AND id<>? ORDER BY id",
                (plan["year"], PlanStatus.APPROVED.value, plan_id),
            )
        ]
        return {
            "plan_id": plan_id,
            "status": plan["status"],
            "feasible": ok,
            "checks": checks,
            "competing_approved_plans": [
                {"plan_id": p["id"], "title": p["title"], "items": p["items"]}
                for p in pending
            ],
        }

    # ------------------------------------------------------------ 正式下达

    def _idem_batch(
        self, idem_key: str, action: str, plan_id: int
    ) -> dict[str, Any] | None:
        """同幂等键的已登记批次：done 直接回放，failed 报错，否则 None。"""
        row = self.conn.execute(
            "SELECT * FROM batches WHERE idem_key=?", (idem_key,)
        ).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["plan_id"] != plan_id:
            raise IdempotencyConflictError(
                "幂等键已用于其他操作", idempotency_key=idem_key
            )
        if row["status"] == BatchStatus.DONE.value:
            return self.get_batch(row["id"])
        if row["status"] == BatchStatus.FAILED.value:
            raise ConflictError(
                "该幂等键对应的批次此前已失败，请更换幂等键重试",
                batch_id=row["id"],
                last_error=row["last_error"],
            )
        return None

    def issue_plan(self, plan_id: int, *, idem_key: str) -> dict[str, Any]:
        """正式下达：原子锁定全部额度，生成不可变清单。"""
        replay = self._idem_batch(idem_key, "issue", plan_id)
        if replay is not None:
            return replay
        plan = self._require_plan(plan_id)
        if plan["status"] != PlanStatus.APPROVED.value:
            raise InvalidStateError(
                "只有已批准方案可以下达",
                plan_id=plan_id,
                status=plan["status"],
            )
        items = self.conn.execute(
            "SELECT kind, amount, account_name FROM plan_items WHERE plan_id=? "
            "ORDER BY id",
            (plan_id,),
        ).fetchall()
        if not items:
            raise ValidationError("方案没有明细，无法下达")
        manifest = [
            {
                "kind": r["kind"],
                "amount": r["amount"],
                "account_name": r["account_name"],
            }
            for r in items
        ]
        return self._register_and_run_batch(
            plan_id=plan_id,
            idem_key=idem_key,
            action="issue",
            reason="",
            manifest=manifest,
            execute=lambda batch_id: self._execute_issue(batch_id, plan_id),
        )

    def _execute_issue(self, batch_id: int, plan_id: int) -> None:
        plan = self._require_plan(plan_id)
        if plan["status"] != PlanStatus.APPROVED.value:
            raise InvalidStateError(
                "方案状态不允许下达", plan_id=plan_id, status=plan["status"]
            )
        project = self.get_project(plan["project_id"])
        manifest = self.conn.execute(
            "SELECT * FROM batch_items WHERE batch_id=? ORDER BY seq", (batch_id,)
        ).fetchall()
        # 恢复续办时已落账的明细不再参与额度校验：其占用已在台账中，
        # 重复计入会造成"自己挤占自己"
        pending = [mi for mi in manifest if mi["status"] != "done"]
        add_by_account: dict[int, dict[str, int]] = {}
        for mi in pending:
            account = self._resolve_account(
                plan["year"], mi["kind"], mi["account_name"]
            )
            tracks = self._account_tracks(account["id"])
            if project["track"] not in tracks:
                raise ConstraintViolationError(
                    "项目赛道不在账户适用范围内",
                    kind=mi["kind"],
                    track=project["track"],
                    applicable=list(tracks),
                )
            used_now = ledger.project_used(self.conn, project["id"], account["id"])
            if (
                account["max_per_project"] is not None
                and used_now + mi["amount"] > account["max_per_project"]
            ):
                raise QuotaExceededError(
                    "超过每项目上限",
                    kind=mi["kind"],
                    used=used_now,
                    requested=mi["amount"],
                    max_per_project=account["max_per_project"],
                )
            add_by_account.setdefault(account["id"], {})
            d = add_by_account[account["id"]]
            d[project["track"]] = d.get(project["track"], 0) + mi["amount"]

        for account_id, additional in add_by_account.items():
            account = self.conn.execute(
                "SELECT * FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
            feasible, detail = self._floor_feasible(
                account, self._account_tracks(account_id), additional
            )
            if not feasible:
                raise QuotaExceededError(
                    detail["reason"], account_id=account_id, capacity=detail
                )

        # 校验全部通过后，逐条锁定：清单 + 台账分录
        for mi in manifest:
            if mi["status"] == "done":
                continue  # 防御性：恢复时跳过已落账明细
            account = self._resolve_account(
                plan["year"], mi["kind"], mi["account_name"]
            )
            cur = self.conn.execute(
                "INSERT INTO allocations(batch_id, item_id, plan_id, project_id, "
                "account_id, kind, track, amount, origin, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    batch_id, mi["id"], plan_id, project["id"], account["id"],
                    mi["kind"], project["track"], mi["amount"], "reserve", utcnow(),
                ),
            )
            allocation_id = int(cur.lastrowid)
            ledger.append_entry(
                self.conn,
                plan_id=plan_id,
                project_id=project["id"],
                account_id=account["id"],
                allocation_id=allocation_id,
                kind=mi["kind"],
                track=project["track"],
                txn_type=TxnType.RESERVE.value,
                amount=mi["amount"],
                ref=f"batch:{batch_id}:item:{mi['seq']}",
                note="正式下达锁定",
            )
            self.conn.execute(
                "UPDATE batch_items SET status='done', allocation_id=? WHERE id=?",
                (allocation_id, mi["id"]),
            )

        self.conn.execute(
            "UPDATE plans SET status=?, updated_at=? WHERE id=?",
            (PlanStatus.ISSUED.value, utcnow(), plan_id),
        )

    def _execute_reject(self, batch_id: int, plan_id: int, reason: str) -> None:
        plan = self._require_plan(plan_id)
        if plan["status"] not in (
            PlanStatus.SUBMITTED.value,
            PlanStatus.APPROVED.value,
        ):
            raise InvalidStateError(
                "方案当前状态不能退回", plan_id=plan_id, status=plan["status"]
            )
        # 尚未下达，没有预占需要释放；状态扭转即完成
        self.conn.execute(
            "UPDATE plans SET status=?, reject_reason=?, updated_at=? WHERE id=?",
            (PlanStatus.REJECTED.value, reason, utcnow(), plan_id),
        )

    def _execute_post_issue_reject(
        self, batch_id: int, plan_id: int, reason: str
    ) -> None:
        """已下达后退回：全部未结清额回收，方案转为退回结清。"""
        plan = self._require_plan(plan_id)
        if plan["status"] not in _LIVE_STATES:
            raise InvalidStateError(
                "方案当前状态不能整单退回", plan_id=plan_id, status=plan["status"]
            )
        manifest = self.conn.execute(
            "SELECT * FROM batch_items WHERE batch_id=? ORDER BY seq", (batch_id,)
        ).fetchall()
        for mi in manifest:
            if mi["status"] == "done":
                continue
            alloc = self.conn.execute(
                "SELECT * FROM allocations WHERE id=?", (mi["allocation_id"],)
            ).fetchone()
            outstanding = ledger.allocation_balance(self.conn, mi["allocation_id"])
            if mi["amount"] > outstanding:
                raise QuotaExceededError(
                    "退回数量超过清单未结清额（可能已被先行调减）",
                    allocation_id=mi["allocation_id"],
                    requested=mi["amount"],
                    outstanding=outstanding,
                )
            ledger.append_entry(
                self.conn,
                plan_id=plan_id,
                project_id=alloc["project_id"],
                account_id=alloc["account_id"],
                allocation_id=alloc["id"],
                kind=alloc["kind"],
                track=alloc["track"],
                txn_type=TxnType.REJECT_RELEASE.value,
                amount=mi["amount"],
                ref=f"batch:{batch_id}:item:{mi['seq']}",
                note=reason,
            )
            self.conn.execute(
                "UPDATE batch_items SET status='done' WHERE id=?", (mi["id"],)
            )
        self.conn.execute(
            "UPDATE plans SET status=?, reject_reason=?, updated_at=? WHERE id=?",
            (PlanStatus.REJECTED.value, reason, utcnow(), plan_id),
        )

    # ------------------------------------------------------------ 部分释放

    def release_resources(
        self,
        plan_id: int,
        *,
        items: list[dict[str, Any]],
        reason: str,
        idem_key: str,
    ) -> dict[str, Any]:
        """只释放/核销部分资源。release 回收额度可再用；write_off 不回收。"""
        # 幂等回放最先处理：方案结清后的重试仍返回首次结果
        replay = self._idem_batch(idem_key, "release", plan_id)
        if replay is not None:
            return replay
        plan = self._require_plan(plan_id)
        if plan["status"] not in _LIVE_STATES:
            raise InvalidStateError(
                "只有已下达方案可以释放资源", plan_id=plan_id, status=plan["status"]
            )
        if not str(reason).strip():
            raise ValidationError("调减必须填写可解释的依据（reason）")
        manifest = self._build_release_manifest(plan_id, items)
        return self._register_and_run_batch(
            plan_id=plan_id,
            idem_key=idem_key,
            action="release",
            reason=reason,
            manifest=manifest,
            execute=lambda batch_id: self._execute_release(batch_id, plan_id, reason),
        )

    def _build_release_manifest(
        self, plan_id: int, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not isinstance(items, list) or not items:
            raise ValidationError("至少一条释放明细")
        out: list[dict[str, Any]] = []
        for it in items:
            allocation_id = it.get("allocation_id")
            amount = it.get("amount")
            mode = it.get("mode", "release")
            if not isinstance(allocation_id, int):
                raise ValidationError("allocation_id 必须是整数")
            if not isinstance(amount, int) or amount <= 0:
                raise ValidationError("释放数量必须是正整数", allocation_id=allocation_id)
            if mode not in ("release", "write_off"):
                raise ValidationError("mode 必须是 release 或 write_off")
            alloc = self.conn.execute(
                "SELECT * FROM allocations WHERE id=? AND plan_id=?",
                (allocation_id, plan_id),
            ).fetchone()
            if alloc is None:
                raise NotFoundError(
                    "清单不存在或不属于该方案",
                    allocation_id=allocation_id,
                    plan_id=plan_id,
                )
            outstanding = ledger.allocation_balance(self.conn, allocation_id)
            if amount > outstanding:
                raise QuotaExceededError(
                    "释放数量超过清单未结清额",
                    allocation_id=allocation_id,
                    requested=amount,
                    outstanding=outstanding,
                )
            out.append(
                {
                    "allocation_id": allocation_id,
                    "kind": alloc["kind"],
                    "amount": amount,
                    "mode": mode,
                }
            )
        return out

    def _execute_release(self, batch_id: int, plan_id: int, reason: str) -> None:
        plan = self._require_plan(plan_id)
        if plan["status"] not in _LIVE_STATES:
            raise InvalidStateError(
                "方案当前状态不能释放", plan_id=plan_id, status=plan["status"]
            )
        manifest = self.conn.execute(
            "SELECT * FROM batch_items WHERE batch_id=? ORDER BY seq", (batch_id,)
        ).fetchall()
        for mi in manifest:
            if mi["status"] == "done":
                continue
            alloc = self.conn.execute(
                "SELECT * FROM allocations WHERE id=?", (mi["allocation_id"],)
            ).fetchone()
            outstanding = ledger.allocation_balance(self.conn, mi["allocation_id"])
            if mi["amount"] > outstanding:
                raise QuotaExceededError(
                    "释放数量超过清单未结清额（可能已被其他批次释放）",
                    allocation_id=mi["allocation_id"],
                    requested=mi["amount"],
                    outstanding=outstanding,
                )
            ledger.append_entry(
                self.conn,
                plan_id=plan_id,
                project_id=alloc["project_id"],
                account_id=alloc["account_id"],
                allocation_id=alloc["id"],
                kind=alloc["kind"],
                track=alloc["track"],
                txn_type=(
                    TxnType.WRITE_OFF.value
                    if mi["mode"] == "write_off"
                    else TxnType.RELEASE.value
                ),
                amount=mi["amount"],
                ref=f"batch:{batch_id}:item:{mi['seq']}",
                note=reason,
            )
            self.conn.execute(
                "UPDATE batch_items SET status='done' WHERE id=?", (mi["id"],)
            )
        self._refresh_plan_status(plan_id)

    # ------------------------------------------------------------ 跨项目调剂

    def transfer_resources(
        self,
        *,
        from_plan_id: int,
        to_plan_id: int,
        items: list[dict[str, Any]],
        reason: str,
        idem_key: str,
    ) -> dict[str, Any]:
        """跨项目调剂：从源方案的未结清清单划出，划入接收方案。"""
        if from_plan_id == to_plan_id:
            raise ValidationError("调剂必须在不同方案（项目）之间进行")
        if not str(reason).strip():
            raise ValidationError("调剂必须填写可解释的依据（reason）")
        src = self._require_plan(from_plan_id)
        dst = self._require_plan(to_plan_id)
        if src["status"] not in _LIVE_STATES:
            raise InvalidStateError(
                "源方案必须已下达", plan_id=from_plan_id, status=src["status"]
            )
        if dst["status"] not in (
            PlanStatus.APPROVED.value,
            PlanStatus.ISSUED.value,
            PlanStatus.PARTIALLY_RELEASED.value,
        ):
            raise InvalidStateError(
                "接收方案必须已批准（尚未拒绝/终结）",
                plan_id=to_plan_id,
                status=dst["status"],
            )
        if dst["status"] == PlanStatus.APPROVED.value:
            own_items = self.conn.execute(
                "SELECT COUNT(*) AS n FROM plan_items WHERE plan_id=?",
                (to_plan_id,),
            ).fetchone()["n"]
            if own_items:
                raise InvalidStateError(
                    "接收方案自带申请明细时须先正式下达，再接受调剂",
                    plan_id=to_plan_id,
                )
        if src["year"] != dst["year"]:
            raise ConstraintViolationError(
                "不能跨年度调剂", from_year=src["year"], to_year=dst["year"]
            )
        # 幂等短路必须在余额校验之前：重放时源清单余额可能已变化
        existing = self.conn.execute(
            "SELECT * FROM transfers WHERE idem_key=?", (idem_key,)
        ).fetchone()
        if existing is not None:
            if (
                existing["from_plan_id"] != from_plan_id
                or existing["to_plan_id"] != to_plan_id
            ):
                raise IdempotencyConflictError(
                    "幂等键已用于其他调剂", idempotency_key=idem_key
                )
            if existing["status"] == TransferStatus.DONE.value:
                return self.get_transfer(existing["id"])
            if existing["status"] == TransferStatus.CANCELLED.value:
                raise ConflictError("该幂等键对应的调剂已取消")
            # PENDING（崩溃残留）：跳过重建清单，直接续办
            self._execute_transfer(existing["id"])
            return self.get_transfer(existing["id"])
        manifest = self._build_transfer_manifest(from_plan_id, items)
        return self._register_and_run_transfer(
            from_plan_id=from_plan_id,
            to_plan_id=to_plan_id,
            idem_key=idem_key,
            reason=reason,
            manifest=manifest,
        )

    def _build_transfer_manifest(
        self, from_plan_id: int, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not isinstance(items, list) or not items:
            raise ValidationError("至少一条调剂明细")
        out: list[dict[str, Any]] = []
        seen: set[int] = set()
        for it in items:
            allocation_id = it.get("allocation_id")
            amount = it.get("amount")
            if not isinstance(allocation_id, int):
                raise ValidationError("allocation_id 必须是整数")
            if not isinstance(amount, int) or amount <= 0:
                raise ValidationError("调剂数量必须是正整数")
            if allocation_id in seen:
                raise ValidationError(
                    "同一清单在一次调剂中只能出现一次", allocation_id=allocation_id
                )
            seen.add(allocation_id)
            alloc = self.conn.execute(
                "SELECT * FROM allocations WHERE id=? AND plan_id=?",
                (allocation_id, from_plan_id),
            ).fetchone()
            if alloc is None:
                raise NotFoundError(
                    "清单不存在或不属于源方案", allocation_id=allocation_id
                )
            outstanding = ledger.allocation_balance(self.conn, allocation_id)
            if amount > outstanding:
                raise QuotaExceededError(
                    "调剂数量超过清单未结清额",
                    allocation_id=allocation_id,
                    requested=amount,
                    outstanding=outstanding,
                )
            out.append({"allocation_id": allocation_id, "amount": amount})
        return out

    def _register_and_run_transfer(
        self,
        *,
        from_plan_id: int,
        to_plan_id: int,
        idem_key: str,
        reason: str,
        manifest: list[dict[str, Any]],
    ) -> dict[str, Any]:
        # 幂等：同键直接回放
        existing = self.conn.execute(
            "SELECT * FROM transfers WHERE idem_key=?", (idem_key,)
        ).fetchone()
        if existing is not None:
            if (
                existing["from_plan_id"] != from_plan_id
                or existing["to_plan_id"] != to_plan_id
            ):
                raise IdempotencyConflictError(
                    "幂等键已用于其他调剂", idempotency_key=idem_key
                )
            if existing["status"] == TransferStatus.DONE.value:
                return self.get_transfer(existing["id"])
            if existing["status"] == TransferStatus.CANCELLED.value:
                raise ConflictError("该幂等键对应的调剂已取消")
            transfer_id = existing["id"]
        else:
            ledger.begin_immediate(self.conn)
            try:
                cur = self.conn.execute(
                    "INSERT INTO transfers(idem_key, from_plan_id, to_plan_id, "
                    "status, reason, created_at) VALUES (?,?,?,?,?,?)",
                    (
                        idem_key, from_plan_id, to_plan_id,
                        TransferStatus.PENDING.value, reason, utcnow(),
                    ),
                )
                transfer_id = int(cur.lastrowid)
                self.conn.executemany(
                    "INSERT INTO transfer_items(transfer_id, seq, "
                    "from_allocation_id, amount) VALUES (?,?,?,?)",
                    [
                        (transfer_id, i, m["allocation_id"], m["amount"])
                        for i, m in enumerate(manifest)
                    ],
                )
                ledger.commit(self.conn)
            except Exception:
                ledger.rollback(self.conn)
                raise

        self._execute_transfer(transfer_id)
        return self.get_transfer(transfer_id)

    def _execute_transfer(self, transfer_id: int) -> None:
        tr = self.conn.execute(
            "SELECT * FROM transfers WHERE id=?", (transfer_id,)
        ).fetchone()
        if tr["status"] == TransferStatus.DONE.value:
            return
        dst = self._require_plan(tr["to_plan_id"])
        dst_project = self.get_project(dst["project_id"])
        items = self.conn.execute(
            "SELECT * FROM transfer_items WHERE transfer_id=? ORDER BY seq",
            (transfer_id,),
        ).fetchall()

        ledger.begin_immediate(self.conn)
        try:
            # 按账户汇总赛道净变化：源赛道为负（划出释放），接收赛道为正
            net_by_account: dict[int, dict[str, int]] = {}
            for ti in items:
                alloc = self.conn.execute(
                    "SELECT * FROM allocations WHERE id=?",
                    (ti["from_allocation_id"],),
                ).fetchone()
                tracks = self._account_tracks(alloc["account_id"])
                if dst_project["track"] not in tracks:
                    raise ConstraintViolationError(
                        "接收项目赛道不在该账户适用范围内，不能调剂此类资源",
                        account_id=alloc["account_id"],
                        track=dst_project["track"],
                    )
                outstanding = ledger.allocation_balance(self.conn, alloc["id"])
                if ti["amount"] > outstanding:
                    raise QuotaExceededError(
                        "调剂数量超过清单未结清额（可能已被其他操作先行调减）",
                        allocation_id=alloc["id"],
                        requested=ti["amount"],
                        outstanding=outstanding,
                    )
                net = net_by_account.setdefault(
                    alloc["account_id"], {alloc["track"]: 0}
                )
                net[alloc["track"]] = net.get(alloc["track"], 0) - ti["amount"]
                net[dst_project["track"]] = (
                    net.get(dst_project["track"], 0) + ti["amount"]
                )

            # 净变化后重算各账户保底可行性（跨赛道调剂不能压破接收方保底）
            for account_id, delta in net_by_account.items():
                account = self.conn.execute(
                    "SELECT * FROM accounts WHERE id=?", (account_id,)
                ).fetchone()
                feasible, detail = self._floor_feasible(
                    account, self._account_tracks(account_id), delta
                )
                if not feasible:
                    raise QuotaExceededError(
                        detail["reason"], account_id=account_id, capacity=detail
                    )

            for ti in items:
                alloc = self.conn.execute(
                    "SELECT * FROM allocations WHERE id=?",
                    (ti["from_allocation_id"],),
                ).fetchone()
                # 划出：源清单释放占用
                ledger.append_entry(
                    self.conn,
                    plan_id=tr["from_plan_id"],
                    project_id=alloc["project_id"],
                    account_id=alloc["account_id"],
                    allocation_id=alloc["id"],
                    kind=alloc["kind"],
                    track=alloc["track"],
                    txn_type=TxnType.TRANSFER_OUT.value,
                    amount=ti["amount"],
                    ref=f"transfer:{transfer_id}:item:{ti['seq']}",
                    note=tr["reason"],
                )
                # 划入：生成接收方案的新不可变清单
                cur = self.conn.execute(
                    "INSERT INTO allocations(batch_id, transfer_id, plan_id, "
                    "project_id, account_id, kind, track, amount, origin, "
                    "created_at) VALUES (NULL,?,?,?,?,?,?,?,?,?)",
                    (
                        transfer_id, tr["to_plan_id"],
                        dst_project["id"], alloc["account_id"], alloc["kind"],
                        dst_project["track"], ti["amount"], "transfer_in", utcnow(),
                    ),
                )
                new_id = int(cur.lastrowid)
                ledger.append_entry(
                    self.conn,
                    plan_id=tr["to_plan_id"],
                    project_id=dst_project["id"],
                    account_id=alloc["account_id"],
                    allocation_id=new_id,
                    kind=alloc["kind"],
                    track=dst_project["track"],
                    txn_type=TxnType.TRANSFER_IN.value,
                    amount=ti["amount"],
                    ref=f"transfer:{transfer_id}:item:{ti['seq']}",
                    note=tr["reason"],
                )

            now = utcnow()
            self.conn.execute(
                "UPDATE transfers SET status='done', finished_at=? WHERE id=?",
                (now, transfer_id),
            )
            # 接收方案若是首次获得资源，直接进入已下达
            if dst["status"] == PlanStatus.APPROVED.value:
                self.conn.execute(
                    "UPDATE plans SET status=?, updated_at=? WHERE id=?",
                    (PlanStatus.ISSUED.value, now, dst["id"]),
                )
            self._refresh_plan_status(tr["from_plan_id"])
            if dst["status"] in _LIVE_STATES:
                self._refresh_plan_status(tr["to_plan_id"])
            ledger.commit(self.conn)
        except Exception:
            ledger.rollback(self.conn)
            raise

    def get_transfer(self, transfer_id: int) -> dict[str, Any]:
        tr = self.conn.execute(
            "SELECT * FROM transfers WHERE id=?", (transfer_id,)
        ).fetchone()
        if tr is None:
            raise NotFoundError("调剂单不存在", transfer_id=transfer_id)
        out = dict(tr)
        out["items"] = [
            dict(r)
            for r in self.conn.execute(
                "SELECT seq, from_allocation_id, amount FROM transfer_items "
                "WHERE transfer_id=? ORDER BY seq",
                (transfer_id,),
            )
        ]
        return out

    # -------------------------------------------------------- 批次与恢复

    def _register_and_run_batch(
        self,
        *,
        plan_id: int,
        idem_key: str,
        action: str,
        reason: str,
        manifest: list[dict[str, Any]],
        execute: Any,
    ) -> dict[str, Any]:
        """通用批次生命周期：登记意图 -> 执行 -> 崩溃可续。"""
        existing = self.conn.execute(
            "SELECT * FROM batches WHERE idem_key=?", (idem_key,)
        ).fetchone()
        if existing is not None:
            if existing["plan_id"] != plan_id or existing["action"] != action:
                raise IdempotencyConflictError(
                    "幂等键已用于其他操作", idempotency_key=idem_key
                )
            if existing["status"] == BatchStatus.DONE.value:
                return self.get_batch(existing["id"])  # 幂等回放
            if existing["status"] == BatchStatus.FAILED.value:
                raise ConflictError(
                    "该幂等键对应的批次此前已失败，请更换幂等键重试",
                    batch_id=existing["id"],
                    last_error=existing["last_error"],
                )
            batch_id = existing["id"]  # pending/running：恢复续办
        else:
            ledger.begin_immediate(self.conn)
            try:
                now = utcnow()
                cur = self.conn.execute(
                    "INSERT INTO batches(idem_key, plan_id, action, reason, "
                    "status, created_at) VALUES (?,?,?,?,?,?)",
                    (idem_key, plan_id, action, reason, BatchStatus.PENDING.value, now),
                )
                batch_id = int(cur.lastrowid)
                for seq, m in enumerate(manifest):
                    self.conn.execute(
                        "INSERT INTO batch_items(batch_id, seq, allocation_id, "
                        "kind, amount, account_name, mode, status) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (
                            batch_id, seq, m.get("allocation_id"),
                            m.get("kind"), m.get("amount"), m.get("account_name"),
                            m.get("mode"), "pending",
                        ),
                    )
                ledger.commit(self.conn)
            except Exception:
                ledger.rollback(self.conn)
                raise

        # 事务 A：认领批次（崩溃后留下 running 痕迹，可被 recover 发现）
        ledger.begin_immediate(self.conn)
        try:
            self.conn.execute(
                "UPDATE batches SET status='running', attempts=attempts+1, "
                "heartbeat=? WHERE id=?",
                (utcnow(), batch_id),
            )
            ledger.commit(self.conn)
        except Exception:
            ledger.rollback(self.conn)
            raise

        # 事务 B：执行落账，整体提交或整体回滚
        try:
            ledger.begin_immediate(self.conn)
            execute(batch_id)
            self.conn.execute(
                "UPDATE batches SET status='done', heartbeat=NULL, "
                "finished_at=? WHERE id=?",
                (utcnow(), batch_id),
            )
            ledger.commit(self.conn)
        except DomainError:
            # 业务规则失败（额度不足、状态不对等）：重试不会成功，永久失败
            ledger.rollback(self.conn)
            self._mark_batch_failed(batch_id)
            raise
        except Exception:
            # 基础设施异常（进程崩溃、锁、IO）：保留 running，由 recover 续办
            ledger.rollback(self.conn)
            raise
        return self.get_batch(batch_id)

    def _mark_batch_failed(self, batch_id: int) -> None:
        import sys

        exc = sys.exc_info()[1]
        ledger.begin_immediate(self.conn)
        try:
            self.conn.execute(
                "UPDATE batches SET status='failed', last_error=?, "
                "heartbeat=NULL WHERE id=?",
                (f"{type(exc).__name__}: {exc}" if exc else "unknown", batch_id),
            )
            ledger.commit(self.conn)
        except Exception:
            ledger.rollback(self.conn)

    def get_batch(self, batch_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM batches WHERE id=?", (batch_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("批次不存在", batch_id=batch_id)
        out = dict(row)
        out["items"] = [
            dict(r)
            for r in self.conn.execute(
                "SELECT seq, allocation_id, kind, amount, account_name, mode, "
                "status, error FROM batch_items WHERE batch_id=? ORDER BY seq",
                (batch_id,),
            )
        ]
        return out

    def recover(self) -> dict[str, Any]:
        """续办服务中断时未完成的批次与调剂单。

        已提交的落账不会重做（status=done 的明细/单据直接跳过），
        未提交的事务在崩溃时已整体回滚，因此续办不会重复扣减。
        """
        resumed: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        rows = self.conn.execute(
            "SELECT * FROM batches WHERE status IN (?,?) ORDER BY id",
            _RUNNABLE_BATCH,
        ).fetchall()
        for row in rows:
            try:
                if row["action"] == "issue":
                    self._register_and_run_batch(
                        plan_id=row["plan_id"],
                        idem_key=row["idem_key"],
                        action="issue",
                        reason="",
                        manifest=[],
                        execute=lambda bid: self._execute_issue(
                            bid, self._batch_plan(bid)
                        ),
                    )
                elif row["action"] == "reject":
                    has_items = self.conn.execute(
                        "SELECT COUNT(*) AS n FROM batch_items WHERE batch_id=?",
                        (row["id"],),
                    ).fetchone()["n"]
                    if has_items:
                        execute_fn = lambda bid: self._execute_post_issue_reject(
                            bid, self._batch_plan(bid), row["reason"]
                        )
                    else:
                        execute_fn = lambda bid: self._execute_reject(
                            bid, self._batch_plan(bid), row["reason"]
                        )
                    self._register_and_run_batch(
                        plan_id=row["plan_id"],
                        idem_key=row["idem_key"],
                        action="reject",
                        reason=row["reason"],
                        manifest=[],
                        execute=execute_fn,
                    )
                else:  # release
                    self._register_and_run_batch(
                        plan_id=row["plan_id"],
                        idem_key=row["idem_key"],
                        action="release",
                        reason=row["reason"],
                        manifest=[],
                        execute=lambda bid: self._execute_release(
                            bid, self._batch_plan(bid), row["reason"]
                        ),
                    )
                resumed.append({"kind": "batch", **self.get_batch(row["id"])})
            except DomainError as exc:
                # 单个批次业务失败（如额度已被合法占用）不阻断其他批次恢复
                failed.append(
                    {
                        "kind": "batch",
                        "batch_id": row["id"],
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

        trs = self.conn.execute(
            "SELECT * FROM transfers WHERE status=? ORDER BY id",
            (TransferStatus.PENDING.value,),
        ).fetchall()
        for tr in trs:
            try:
                self._execute_transfer(tr["id"])
                resumed.append({"kind": "transfer", **self.get_transfer(tr["id"])})
            except DomainError as exc:
                failed.append(
                    {
                        "kind": "transfer",
                        "transfer_id": tr["id"],
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        return {"resumed": resumed, "failed": failed}

    def _batch_plan(self, batch_id: int) -> int:
        row = self.conn.execute(
            "SELECT plan_id FROM batches WHERE id=?", (batch_id,)
        ).fetchone()
        return int(row["plan_id"])

    # ------------------------------------------------------------ 查询/对账

    def _refresh_plan_status(self, plan_id: int) -> None:
        """依据清单未结清额重算已下达方案状态（调用方须持事务）。"""
        plan = self._require_plan(plan_id)
        if plan["status"] not in _LIVE_STATES:
            return
        row = self.conn.execute(
            "SELECT COALESCE(SUM(outstanding_delta),0) AS outstanding, "
            "COUNT(*) AS n FROM ledger_entries WHERE plan_id=?",
            (plan_id,),
        ).fetchone()
        has_adjustment = self.conn.execute(
            "SELECT 1 FROM ledger_entries WHERE plan_id=? AND txn_type IN "
            "('release','write_off','transfer_out') LIMIT 1",
            (plan_id,),
        ).fetchone()
        if row["outstanding"] == 0:
            new_status = PlanStatus.CLOSED.value
        elif has_adjustment:
            new_status = PlanStatus.PARTIALLY_RELEASED.value
        else:
            new_status = PlanStatus.ISSUED.value
        if new_status != plan["status"]:
            self.conn.execute(
                "UPDATE plans SET status=?, updated_at=? WHERE id=?",
                (new_status, utcnow(), plan_id),
            )

    def list_allocations(self, plan_id: int) -> list[dict[str, Any]]:
        self._require_plan(plan_id)
        out = []
        for r in self.conn.execute(
            "SELECT * FROM allocations WHERE plan_id=? ORDER BY id", (plan_id,)
        ):
            item = dict(r)
            item["outstanding"] = ledger.allocation_balance(self.conn, r["id"])
            item["events"] = [
                {
                    "id": e["id"],
                    "txn_type": e["txn_type"],
                    "amount": e["amount"],
                    "ref": e["ref"],
                    "note": e["note"],
                    "created_at": e["created_at"],
                }
                for e in ledger.allocation_events(self.conn, r["id"])
            ]
            out.append(item)
        return out

    def list_ledger_entries(
        self, *, account_id: int | None = None, plan_id: int | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM ledger_entries WHERE 1=1"
        params: list[Any] = []
        if account_id is not None:
            sql += " AND account_id=?"
            params.append(account_id)
        if plan_id is not None:
            sql += " AND plan_id=?"
            params.append(plan_id)
        sql += " ORDER BY id"
        return [dict(r) for r in self.conn.execute(sql, params)]

    def reconcile(self) -> dict[str, Any]:
        """账实一致性核对：哈希链、额度非负、清单余额、方案状态。"""
        ledger.verify_chain(self.conn)
        accounts_report: list[dict[str, Any]] = []
        for acct in self.conn.execute("SELECT * FROM accounts ORDER BY id"):
            used = ledger.total_used(self.conn, acct["id"])
            accounts_report.append(
                {
                    "account_id": acct["id"],
                    "total_amount": acct["total_amount"],
                    "used_amount": used,
                    "available_amount": acct["total_amount"] - used,
                    "balanced": 0 <= used <= acct["total_amount"],
                }
            )
        allocations_report = []
        for a in self.conn.execute("SELECT * FROM allocations ORDER BY id"):
            outstanding = ledger.allocation_balance(self.conn, a["id"])
            if not 0 <= outstanding <= a["amount"]:
                raise LedgerIntegrityError(
                    "清单未结清额越界",
                    allocation_id=a["id"],
                    outstanding=outstanding,
                    amount=a["amount"],
                )
            allocations_report.append(
                {"allocation_id": a["id"], "outstanding": outstanding}
            )
        # 方案状态与余额一致
        for p in self.conn.execute("SELECT * FROM plans ORDER BY id"):
            if p["status"] in _LIVE_STATES or p["status"] == PlanStatus.CLOSED.value:
                row = self.conn.execute(
                    "SELECT COALESCE(SUM(outstanding_delta),0) AS bal FROM "
                    "ledger_entries WHERE plan_id=?",
                    (p["id"],),
                ).fetchone()
                if p["status"] == PlanStatus.CLOSED.value and row["bal"] != 0:
                    raise LedgerIntegrityError(
                        "已结清方案仍有未结清额", plan_id=p["id"], outstanding=row["bal"]
                    )
        return {
            "hash_chain": "ok",
            "accounts": accounts_report,
            "allocations_checked": len(allocations_report),
            "balanced": all(a["balanced"] for a in accounts_report),
        }
