"""退回、部分释放（含核销）、跨项目调剂。"""
from __future__ import annotations

from _service_case import ServiceCase
from resource_allocation.errors import (
    ConstraintViolationError,
    InvalidStateError,
    NotFoundError,
    QuotaExceededError,
    ValidationError,
)
from resource_allocation.models import PlanStatus


class ReleaseTest(ServiceCase):
    def test_partial_release_returns_quota(self) -> None:
        world = self.setup_world()
        plan = self.make_issued_plan("1", "P-AI", 3_000_000, "1")
        aid = self.svc.list_allocations(plan["id"])[0]["id"]
        self.svc.release_resources(
            plan["id"],
            items=[{"allocation_id": aid, "amount": 1_000_000, "mode": "release"}],
            reason="设备采购核减",
            idem_key="r1",
        )
        snap = self.svc.account_snapshot(world["account"]["id"])
        self.assertEqual(snap["used_amount"], 2_000_000)
        self.assertEqual(snap["available_amount"], 8_000_000)
        self.assertEqual(
            self.svc.get_plan(plan["id"])["status"],
            PlanStatus.PARTIALLY_RELEASED.value,
        )
        alloc = self.svc.list_allocations(plan["id"])[0]
        self.assertEqual(alloc["outstanding"], 2_000_000)
        types = [e["txn_type"] for e in alloc["events"]]
        self.assertEqual(types, ["reserve", "release"])

    def test_release_is_idempotent(self) -> None:
        self.setup_world()
        plan = self.make_issued_plan("1", "P-AI", 3_000_000, "1")
        aid = self.svc.list_allocations(plan["id"])[0]["id"]
        payload = dict(
            items=[{"allocation_id": aid, "amount": 1_000_000, "mode": "release"}],
            reason="核减",
            idem_key="r1",
        )
        self.svc.release_resources(plan["id"], **payload)
        self.svc.release_resources(plan["id"], **payload)
        alloc = self.svc.list_allocations(plan["id"])[0]
        self.assertEqual(alloc["outstanding"], 2_000_000)
        self.assertEqual(len(alloc["events"]), 2)

    def test_cannot_release_more_than_outstanding(self) -> None:
        self.setup_world()
        plan = self.make_issued_plan("1", "P-AI", 3_000_000, "1")
        aid = self.svc.list_allocations(plan["id"])[0]["id"]
        with self.assertRaises(QuotaExceededError):
            self.svc.release_resources(
                plan["id"],
                items=[{"allocation_id": aid, "amount": 9_000_000, "mode": "release"}],
                reason="x",
                idem_key="r1",
            )

    def test_write_off_does_not_return_quota(self) -> None:
        world = self.setup_world()
        plan = self.make_issued_plan("1", "P-AI", 3_000_000, "1")
        aid = self.svc.list_allocations(plan["id"])[0]["id"]
        self.svc.release_resources(
            plan["id"],
            items=[{"allocation_id": aid, "amount": 1_000_000, "mode": "write_off"}],
            reason="已据实支出，核销",
            idem_key="w1",
        )
        snap = self.svc.account_snapshot(world["account"]["id"])
        # 核销仍占用年度额度（不可再分），但清单未结清额下降
        self.assertEqual(snap["used_amount"], 3_000_000)
        self.assertEqual(snap["available_amount"], 7_000_000)
        self.assertEqual(
            self.svc.list_allocations(plan["id"])[0]["outstanding"], 2_000_000
        )

    def test_release_requires_reason(self) -> None:
        self.setup_world()
        plan = self.make_issued_plan("1", "P-AI", 100, "1")
        aid = self.svc.list_allocations(plan["id"])[0]["id"]
        with self.assertRaises(ValidationError):
            self.svc.release_resources(
                plan["id"],
                items=[{"allocation_id": aid, "amount": 10, "mode": "release"}],
                reason="   ",
                idem_key="r1",
            )

    def test_release_from_wrong_plan_not_found(self) -> None:
        self.setup_world()
        plan = self.make_issued_plan("1", "P-AI", 100, "1")
        other = self.make_issued_plan("2", "P-CHIP", 50, "2")
        aid = self.svc.list_allocations(plan["id"])[0]["id"]
        with self.assertRaises(NotFoundError):
            self.svc.release_resources(
                other["id"],
                items=[{"allocation_id": aid, "amount": 10, "mode": "release"}],
                reason="x",
                idem_key="r1",
            )

    def test_release_all_closes_plan(self) -> None:
        self.setup_world()
        plan = self.make_issued_plan("1", "P-AI", 100, "1")
        aid = self.svc.list_allocations(plan["id"])[0]["id"]
        self.svc.release_resources(
            plan["id"],
            items=[{"allocation_id": aid, "amount": 100, "mode": "release"}],
            reason="全部收回",
            idem_key="r1",
        )
        self.assertEqual(self.svc.get_plan(plan["id"])["status"], PlanStatus.CLOSED.value)
        # 已结清不能重复释放
        with self.assertRaises(InvalidStateError):
            self.svc.release_resources(
                plan["id"],
                items=[{"allocation_id": aid, "amount": 1, "mode": "release"}],
                reason="x",
                idem_key="r2",
            )

    def test_release_replay_after_plan_closed(self) -> None:
        """方案已结清后，客户端用原幂等键重试必须返回首次批次而非报错。"""
        self.setup_world()
        plan = self.make_issued_plan("1", "P-AI", 100, "1")
        aid = self.svc.list_allocations(plan["id"])[0]["id"]
        payload = dict(
            items=[{"allocation_id": aid, "amount": 100, "mode": "release"}],
            reason="全部收回", idem_key="r1",
        )
        first = self.svc.release_resources(plan["id"], **payload)
        self.assertEqual(
            self.svc.get_plan(plan["id"])["status"], PlanStatus.CLOSED.value
        )
        # 网络重试在结清之后到达：仍然幂等成功
        replay = self.svc.release_resources(plan["id"], **payload)
        self.assertEqual(replay["id"], first["id"])
        events = self.svc.list_allocations(plan["id"])[0]["events"]
        self.assertEqual(len(events), 2)  # reserve + 单次 release


class PostIssueRejectTest(ServiceCase):
    def test_full_reject_after_issue_returns_all_outstanding(self) -> None:
        world = self.setup_world()
        plan = self.make_issued_plan("1", "P-AI", 3_000_000, "1")
        aid = self.svc.list_allocations(plan["id"])[0]["id"]
        self.svc.release_resources(
            plan["id"],
            items=[{"allocation_id": aid, "amount": 1_000_000, "mode": "release"}],
            reason="先期核减",
            idem_key="r1",
        )
        self.svc.reject_plan(plan["id"], reason="重复申报，剩余全部退回", idem_key="rj")
        snap = self.svc.account_snapshot(world["account"]["id"])
        self.assertEqual(snap["used_amount"], 0)
        self.assertEqual(snap["available_amount"], 10_000_000)
        self.assertEqual(self.svc.get_plan(plan["id"])["status"], "rejected")
        self.assertEqual(
            self.svc.list_allocations(plan["id"])[0]["outstanding"], 0
        )
        events = [e["txn_type"] for e in self.svc.list_allocations(plan["id"])[0]["events"]]
        self.assertEqual(events, ["reserve", "release", "reject_release"])

    def test_reject_requires_reason(self) -> None:
        self.setup_world()
        plan = self.make_issued_plan("1", "P-AI", 100, "1")
        with self.assertRaises(ValidationError):
            self.svc.reject_plan(plan["id"], reason="", idem_key="rj")


class TransferTest(ServiceCase):
    def _source_and_target(self, src_amount: int = 3_000_000):
        self.setup_world()
        src = self.make_issued_plan("s", "P-AI", src_amount, "s")
        self.svc.create_project(code="P-C", name="高校C项目", track="AI")
        dst = self.svc.create_plan(
            project_code="P-C", year=2026, title="接收方案", items=[],
            idempotency_key="dst",
        )
        self.svc.submit_plan(dst["id"])
        self.svc.approve_plan(dst["id"])
        return src, self.svc.get_plan(dst["id"])

    def test_transfer_moves_quota_between_projects(self) -> None:
        world = self.setup_world()
        src = self.make_issued_plan("s", "P-AI", 3_000_000, "s")
        self.svc.create_project(code="P-C", name="C", track="AI")
        dst = self.svc.create_plan(
            project_code="P-C", year=2026, title="接收", items=[],
            idempotency_key="dst",
        )
        self.svc.submit_plan(dst["id"])
        self.svc.approve_plan(dst["id"])
        aid = self.svc.list_allocations(src["id"])[0]["id"]
        tr = self.svc.transfer_resources(
            from_plan_id=src["id"], to_plan_id=dst["id"],
            items=[{"allocation_id": aid, "amount": 1_000_000}],
            reason="统筹调剂给C校", idem_key="t1",
        )
        self.assertEqual(tr["status"], "done")
        # 账户总占用不变（资源只是换了项目）
        snap = self.svc.account_snapshot(world["account"]["id"])
        self.assertEqual(snap["used_amount"], 3_000_000)
        self.assertEqual(snap["used_by_track"], {"AI": 3_000_000})
        # 源清单减少、目标清单新增（origin=transfer_in）
        self.assertEqual(self.svc.list_allocations(src["id"])[0]["outstanding"], 2_000_000)
        dst_allocs = self.svc.list_allocations(dst["id"])
        self.assertEqual(len(dst_allocs), 1)
        self.assertEqual(dst_allocs[0]["amount"], 1_000_000)
        self.assertEqual(dst_allocs[0]["origin"], "transfer_in")
        self.assertEqual(self.svc.get_plan(dst["id"])["status"], "issued")

    def test_transfer_idempotent(self) -> None:
        src, dst = self._source_and_target()
        aid = self.svc.list_allocations(src["id"])[0]["id"]
        kwargs = dict(
            from_plan_id=src["id"], to_plan_id=dst["id"],
            items=[{"allocation_id": aid, "amount": 1_000_000}],
            reason="r", idem_key="t1",
        )
        t1 = self.svc.transfer_resources(**kwargs)
        t2 = self.svc.transfer_resources(**kwargs)
        self.assertEqual(t1["id"], t2["id"])
        self.assertEqual(len(self.svc.list_allocations(dst["id"])), 1)
        self.assertEqual(self.svc.list_allocations(src["id"])[0]["outstanding"], 2_000_000)

    def test_transfer_cannot_exceed_outstanding(self) -> None:
        src, dst = self._source_and_target()
        aid = self.svc.list_allocations(src["id"])[0]["id"]
        with self.assertRaises(QuotaExceededError):
            self.svc.transfer_resources(
                from_plan_id=src["id"], to_plan_id=dst["id"],
                items=[{"allocation_id": aid, "amount": 9_000_000}],
                reason="r", idem_key="t1",
            )

    def test_transfer_blocked_by_track_applicability(self) -> None:
        self.setup_world()
        src = self.make_issued_plan("s", "P-AI", 3_000_000, "s")
        # 生物赛道不在账户适用范围内
        self.svc.create_project(code="P-BIO", name="生物校", track="生物")
        dst = self.svc.create_plan(
            project_code="P-BIO", year=2026, title="接收", items=[],
            idempotency_key="dst",
        )
        self.svc.submit_plan(dst["id"])
        self.svc.approve_plan(dst["id"])
        aid = self.svc.list_allocations(src["id"])[0]["id"]
        with self.assertRaises(ConstraintViolationError):
            self.svc.transfer_resources(
                from_plan_id=src["id"], to_plan_id=dst["id"],
                items=[{"allocation_id": aid, "amount": 100}],
                reason="r", idem_key="t1",
            )
        # 划出失败：源余额不变、账实平衡
        self.assertEqual(
            self.svc.list_allocations(src["id"])[0]["outstanding"], 3_000_000
        )

    def test_transfer_cannot_cross_years(self) -> None:
        self.setup_world()
        src = self.make_issued_plan("s", "P-AI", 100, "s")
        self.svc.create_project(code="P-C", name="C", track="AI")
        dst = self.svc.create_plan(
            project_code="P-C", year=2027, title="下一年", items=[],
            idempotency_key="dst",
        )
        self.svc.submit_plan(dst["id"])
        self.svc.approve_plan(dst["id"])
        aid = self.svc.list_allocations(src["id"])[0]["id"]
        with self.assertRaises(ConstraintViolationError):
            self.svc.transfer_resources(
                from_plan_id=src["id"], to_plan_id=dst["id"],
                items=[{"allocation_id": aid, "amount": 10}],
                reason="r", idem_key="t1",
            )

    def test_transfer_requires_reason(self) -> None:
        src, dst = self._source_and_target(100)
        aid = self.svc.list_allocations(src["id"])[0]["id"]
        with self.assertRaises(ValidationError):
            self.svc.transfer_resources(
                from_plan_id=src["id"], to_plan_id=dst["id"],
                items=[{"allocation_id": aid, "amount": 10}],
                reason="", idem_key="t1",
            )

    def test_cross_track_transfer_cannot_break_source_floor(self) -> None:
        """跨赛道调剂：AI→芯片，但划出过多会使 AI 跌破保底，必须拒绝。"""
        world = self.setup_world(total=10_000_000)
        ai = self.make_issued_plan("ai", "P-AI", 6_000_000, "ai")
        chip = self.make_issued_plan("chip", "P-CHIP", 4_000_000, "chip")
        # 各赛道恰好达到保底 40%：AI 600万、芯片 400万
        snap = self.svc.account_snapshot(world["account"]["id"])
        self.assertEqual(
            snap["used_by_track"], {"AI": 6_000_000, "芯片": 4_000_000}
        )
        aid = self.svc.list_allocations(ai["id"])[0]["id"]
        # 划出 300 万给芯片：AI 变 300 万 < 保底 400 万 -> 拒绝
        with self.assertRaises(QuotaExceededError):
            self.svc.transfer_resources(
                from_plan_id=ai["id"], to_plan_id=chip["id"],
                items=[{"allocation_id": aid, "amount": 3_000_000}],
                reason="跨赛道大调剂", idem_key="t-cross",
            )
        # 划出 100 万：AI 500万、芯片 500万，均不低于保底 -> 允许
        tr = self.svc.transfer_resources(
            from_plan_id=ai["id"], to_plan_id=chip["id"],
            items=[{"allocation_id": aid, "amount": 1_000_000}],
            reason="跨赛道小调剂", idem_key="t-cross-ok",
        )
        self.assertEqual(tr["status"], "done")
        snap = self.svc.account_snapshot(world["account"]["id"])
        self.assertEqual(
            snap["used_by_track"], {"AI": 5_000_000, "芯片": 5_000_000}
        )
