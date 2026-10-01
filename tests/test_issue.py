"""正式下达、原子锁定、幂等重放与并发抢占。"""
from __future__ import annotations

import threading

from _service_case import ServiceCase
from resource_allocation.errors import (
    IdempotencyConflictError,
    InvalidStateError,
    QuotaExceededError,
)
from resource_allocation.models import BatchStatus, PlanStatus
from resource_allocation.service import ResourceService


class IssueTest(ServiceCase):
    def test_issue_locks_quota_and_creates_immutable_list(self) -> None:
        world = self.setup_world()
        plan = self.make_issued_plan("1", "P-AI", 3_000_000, "1")
        snap = self.svc.account_snapshot(world["account"]["id"])
        self.assertEqual(snap["used_amount"], 3_000_000)
        self.assertEqual(snap["available_amount"], 7_000_000)
        self.assertEqual(plan["status"], PlanStatus.ISSUED.value)
        allocs = self.svc.list_allocations(plan["id"])
        self.assertEqual(len(allocs), 1)
        self.assertEqual(allocs[0]["amount"], 3_000_000)
        self.assertEqual(allocs[0]["origin"], "reserve")
        self.assertEqual(allocs[0]["outstanding"], 3_000_000)
        # 下达后明细不可改
        with self.assertRaises(InvalidStateError):
            self.svc.update_plan_items(
                plan["id"], [{"kind": "fund", "amount": 1}]
            )

    def test_issue_is_idempotent(self) -> None:
        self.setup_world()
        plan = self.make_issued_plan("1", "P-AI", 3_000_000, "1")
        before = self.svc.list_ledger_entries()
        # 网络重试：相同幂等键返回同一批次，不产生第二条扣减
        batch = self.svc.issue_plan(plan["id"], idem_key="issue-1")
        again = self.svc.issue_plan(plan["id"], idem_key="issue-1")
        self.assertEqual(batch["id"], again["id"])
        self.assertEqual(
            len(self.svc.list_ledger_entries()), len(before)
        )

    def test_same_key_different_plan_conflicts(self) -> None:
        self.setup_world()
        p1 = self.make_issued_plan("1", "P-AI", 100, "1")
        p2 = self.svc.create_plan(
            project_code="P-CHIP", year=2026, title="x",
            items=[{"kind": "fund", "amount": 100}], idempotency_key="p2",
        )
        self.svc.submit_plan(p2["id"])
        self.svc.approve_plan(p2["id"])
        with self.assertRaises(IdempotencyConflictError):
            self.svc.issue_plan(p2["id"], idem_key="issue-1")
        _ = p1

    def test_over_quota_issue_fails_atomically(self) -> None:
        world = self.setup_world()
        plan = self.svc.create_plan(
            project_code="P-AI", year=2026, title="过大",
            items=[{"kind": "fund", "amount": 12_000_000}], idempotency_key="k",
        )
        self.svc.submit_plan(plan["id"])
        self.svc.approve_plan(plan["id"])
        with self.assertRaises(QuotaExceededError):
            self.svc.issue_plan(plan["id"], idem_key="i")
        # 无任何残留占用、无清单
        self.assertEqual(
            self.svc.account_snapshot(world["account"]["id"])["used_amount"], 0
        )
        self.assertEqual(self.svc.list_allocations(plan["id"]), [])
        # 批次标记 failed，方案仍 approved，可修改/重试（用新幂等键）
        batch = self.svc.list_plans()  # noqa: F841
        self.assertEqual(self.svc.get_plan(plan["id"])["status"], "approved")

    def test_second_issue_blocked_after_quota_consumed(self) -> None:
        """两个竞争方案先后下达：额度只够一个，第二个必须失败且不超用。"""
        world = self.setup_world(total=5_000_000)
        a = self.make_issued_plan("a", "P-AI", 3_000_000, "a")
        b = self.svc.create_plan(
            project_code="P-CHIP", year=2026, title="b",
            items=[{"kind": "fund", "amount": 3_000_000}], idempotency_key="b",
        )
        self.svc.submit_plan(b["id"])
        self.svc.approve_plan(b["id"])
        with self.assertRaises(QuotaExceededError):
            self.svc.issue_plan(b["id"], idem_key="ib")
        snap = self.svc.account_snapshot(world["account"]["id"])
        self.assertEqual(snap["used_amount"], 3_000_000)
        self.assertGreaterEqual(snap["available_amount"], 0)
        _ = a

    def test_concurrent_issues_do_not_double_spend(self) -> None:
        """并发下达：写锁串行化，总占用绝不超过总额度。

        使用文件数据库上的两个独立连接，真实模拟两个工作进程竞争。
        """
        import tempfile
        from pathlib import Path

        from resource_allocation.db import connect as db_connect
        from resource_allocation.db import init_db

        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        tmp.close()
        path = Path(tmp.name)
        self.addCleanup(lambda: path.unlink(missing_ok=True))

        setup_conn = db_connect(path)
        init_db(setup_conn)
        setup_svc = ResourceService(setup_conn)
        setup_svc.create_account(
            year=2026, kind="fund", name="中央专项", total_amount=5_000_000,
            tracks=[
                {"track": "AI", "guarantee_bps": 4000},
                {"track": "芯片", "guarantee_bps": 4000},
            ],
        )
        setup_svc.create_project(code="P-AI", name="a", track="AI")
        setup_svc.create_project(code="P-CHIP", name="b", track="芯片")
        plan_ids = []
        for i, code in enumerate(("P-AI", "P-CHIP")):
            p = setup_svc.create_plan(
                project_code=code, year=2026, title=f"p{i}",
                items=[{"kind": "fund", "amount": 3_000_000}],
                idempotency_key=f"p{i}",
            )
            setup_svc.submit_plan(p["id"])
            setup_svc.approve_plan(p["id"])
            plan_ids.append(p["id"])
        setup_conn.close()

        results: list[object] = []

        def worker(plan_id: int, key: str) -> None:
            conn = db_connect(path)
            try:
                svc = ResourceService(conn)
                svc.issue_plan(plan_id, idem_key=key)
                results.append("ok")
            except BaseException as exc:  # noqa: BLE001
                results.append(exc)
            finally:
                conn.close()

        threads = [
            threading.Thread(target=worker, args=(plan_ids[0], "c0")),
            threading.Thread(target=worker, args=(plan_ids[1], "c1")),
        ]
        # 屏障：尽量让两个 BEGIN IMMEDIATE 同时发生
        barrier = threading.Barrier(2)
        orig = worker

        def w2(plan_id: int, key: str) -> None:
            barrier.wait()
            orig(plan_id, key)

        threads = [
            threading.Thread(target=w2, args=(plan_ids[0], "c0")),
            threading.Thread(target=w2, args=(plan_ids[1], "c1")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        oks = [r for r in results if r == "ok"]
        errs = [r for r in results if isinstance(r, QuotaExceededError)]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(errs), 1, results)
        check = db_connect(path)
        snap = ResourceService(check).account_snapshot(1)
        self.assertEqual(snap["used_amount"], 3_000_000)
        self.assertEqual(snap["available_amount"], 2_000_000)
        check.close()

    def test_batch_statuses_recorded(self) -> None:
        self.setup_world()
        plan = self.svc.create_plan(
            project_code="P-AI", year=2026, title="p",
            items=[{"kind": "fund", "amount": 100}], idempotency_key="pk",
        )
        self.svc.submit_plan(plan["id"])
        self.svc.approve_plan(plan["id"])
        batch = self.svc.issue_plan(plan["id"], idem_key="x")
        self.assertEqual(batch["status"], BatchStatus.DONE.value)
        self.assertTrue(all(i["status"] == "done" for i in batch["items"]))
        self.assertIsNotNone(batch["finished_at"])

    def test_unapproved_plan_cannot_issue(self) -> None:
        self.setup_world()
        plan = self.svc.create_plan(
            project_code="P-AI", year=2026, title="p",
            items=[{"kind": "fund", "amount": 100}], idempotency_key="pk",
        )
        self.svc.submit_plan(plan["id"])
        with self.assertRaises(InvalidStateError):
            self.svc.issue_plan(plan["id"], idem_key="x")
        self.assertEqual(
            self.svc.account_snapshot(1)["used_amount"], 0
        )

    def test_failed_batch_recorded_with_error(self) -> None:
        self.setup_world()
        plan = self.svc.create_plan(
            project_code="P-AI", year=2026, title="过大",
            items=[{"kind": "fund", "amount": 99_000_000}], idempotency_key="pk",
        )
        self.svc.submit_plan(plan["id"])
        self.svc.approve_plan(plan["id"])
        with self.assertRaises(QuotaExceededError):
            self.svc.issue_plan(plan["id"], idem_key="x")
        failed = self.svc.get_batch(1)
        self.assertEqual(failed["status"], BatchStatus.FAILED.value)
        self.assertIn("QuotaExceededError", failed["last_error"])
