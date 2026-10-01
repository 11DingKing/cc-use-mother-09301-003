"""核心服务回归测试：建账、试算隔离、原子下达、退回释放、调剂、恢复续办。"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from allocation import AllocationService
from allocation.errors import QuotaError, StateError, ValidationError


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "alloc.db")
        self.svc = AllocationService(self.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # 造数助手
    # ------------------------------------------------------------------
    def make_pool(self, **overrides):
        params = dict(
            year=2026,
            resource_type="fund",
            tracks=["理工"],
            unit="元",
            budget_amount=1_000_000,
            floor_ratio_bp=2000,
            restrictions={},
            actor="财政专员",
        )
        params.update(overrides)
        return self.svc.create_pool(**params)

    def make_app(self, track="理工", applicant="甲大学", title="平台建设"):
        return self.svc.create_application(
            applicant=applicant,
            track=track,
            title=title,
            demands=[{"resource_type": "fund", "amount": 100}],
            actor="高校经办人",
        )

    def make_issued(self, pool_id, app_id, amount):
        batch = self.svc.create_batch(
            actor="财政专员",
            year=2026,
            lines=[{"application_id": app_id, "pool_id": pool_id, "amount": amount}],
        )
        self.svc.approve_batch(batch_id=batch["batch_id"], actor="教育部门")
        result = self.svc.issue_batch(batch_id=batch["batch_id"], actor="教育部门")
        assert result["status"] == "issued", result
        return batch["batch_id"]

    # ------------------------------------------------------------------
    # 建账
    # ------------------------------------------------------------------
    def test_pool_accounts_budget_floor_and_restrictions(self):
        pool = self.make_pool(restrictions={"max_single_amount": 300_000})
        self.assertEqual(pool["budget_amount"], 1_000_000)
        self.assertEqual(pool["floor_amount"], 200_000)  # 保底 20%
        self.assertEqual(pool["available_amount"], 1_000_000)
        self.assertEqual(pool["transferable_amount"], 0)
        self.assertEqual(pool["tracks"], ["理工"])

    def test_pool_validation(self):
        with self.assertRaises(ValidationError):
            self.make_pool(resource_type="gold")
        with self.assertRaises(ValidationError):
            self.make_pool(tracks=[])
        with self.assertRaises(ValidationError):
            self.make_pool(floor_ratio=1.5)
        with self.assertRaises(ValidationError):
            self.make_pool(budget_amount=-1)

    # ------------------------------------------------------------------
    # 试算隔离与方案比较
    # ------------------------------------------------------------------
    def test_plan_simulation_is_isolated_and_comparable(self):
        pool = self.make_pool()
        app_a = self.make_app(applicant="甲大学")
        app_b = self.make_app(applicant="乙大学")
        plan1 = self.svc.create_plan(name="方案一", year=2026, actor="教育部门")
        plan2 = self.svc.create_plan(name="方案二", year=2026, actor="教育部门")
        self.svc.add_plan_item(
            plan_id=plan1["plan_id"],
            application_id=app_a["application_id"],
            pool_id=pool["pool_id"],
            amount=600_000,
        )
        self.svc.add_plan_item(
            plan_id=plan2["plan_id"],
            application_id=app_b["application_id"],
            pool_id=pool["pool_id"],
            amount=1_200_000,  # 超预算：试算允许，标记不可行
        )
        sim1 = self.svc.simulate_plan(plan1["plan_id"])
        sim2 = self.svc.simulate_plan(plan2["plan_id"])
        self.assertTrue(sim1["feasible"])
        self.assertFalse(sim2["feasible"])
        self.assertEqual(sim1["pools"][0]["projected_available"], 400_000)
        self.assertTrue(sim2["pools"][0]["overcommitted"])
        # 试算不碰真账
        self.assertEqual(self.svc.get_pool(pool["pool_id"])["locked_amount"], 0)
        comparison = self.svc.compare_plans([plan1["plan_id"], plan2["plan_id"]])
        amounts = comparison["pools"][0]["plan_amounts"]
        self.assertEqual(amounts[plan1["plan_id"]], 600_000)
        self.assertEqual(amounts[plan2["plan_id"]], 1_200_000)

    def test_plan_item_year_and_track_validation(self):
        pool = self.make_pool()
        other_year_pool = self.make_pool(year=2027)
        app = self.make_app()
        wrong_track_app = self.make_app(track="医学", applicant="丙大学")
        plan = self.svc.create_plan(name="方案", year=2026, actor="教育部门")
        with self.assertRaises(ValidationError):
            self.svc.add_plan_item(
                plan_id=plan["plan_id"],
                application_id=app["application_id"],
                pool_id=other_year_pool["pool_id"],
                amount=1,
            )
        with self.assertRaises(ValidationError):
            self.svc.add_plan_item(
                plan_id=plan["plan_id"],
                application_id=wrong_track_app["application_id"],
                pool_id=pool["pool_id"],
                amount=1,
            )

    # ------------------------------------------------------------------
    # 原子下达与不可变清单
    # ------------------------------------------------------------------
    def test_issue_locks_atomically_and_manifest_is_immutable(self):
        pool = self.make_pool()
        app = self.make_app()
        plan = self.svc.create_plan(name="方案", year=2026, actor="教育部门")
        self.svc.add_plan_item(
            plan_id=plan["plan_id"],
            application_id=app["application_id"],
            pool_id=pool["pool_id"],
            amount=400_000,
        )
        batch = self.svc.create_batch(actor="财政专员", plan_id=plan["plan_id"])
        # 未审批不能下达
        with self.assertRaises(StateError):
            self.svc.issue_batch(batch_id=batch["batch_id"], actor="教育部门")
        self.svc.approve_batch(batch_id=batch["batch_id"], actor="教育部门")
        result = self.svc.issue_batch(batch_id=batch["batch_id"], actor="教育部门")
        self.assertEqual(result["status"], "issued")
        view = self.svc.get_pool(pool["pool_id"])
        self.assertEqual(view["locked_amount"], 400_000)
        self.assertEqual(view["available_amount"], 600_000)
        # 不可变清单：摘要稳定、已入账行不可调减、台账禁改禁删
        manifest1 = self.svc.batch_manifest(batch["batch_id"])
        manifest2 = self.svc.batch_manifest(batch["batch_id"])
        self.assertTrue(manifest1["complete"])
        self.assertEqual(manifest1["digest"], manifest2["digest"])
        self.assertEqual(manifest1["lines"][0]["amount"], 400_000)
        with self.assertRaises(StateError):
            self.svc.adjust_line(
                batch_id=batch["batch_id"],
                line_id=manifest1["lines"][0]["line_id"],
                new_amount=1,
                reason="试图篡改",
                actor="财政专员",
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.svc.db.conn.execute("UPDATE ledger_entry SET amount = 0")
        with self.assertRaises(sqlite3.IntegrityError):
            self.svc.db.conn.execute("DELETE FROM ledger_entry")
        # 方案已转换，不能再改
        with self.assertRaises(StateError):
            self.svc.add_plan_item(
                plan_id=plan["plan_id"],
                application_id=app["application_id"],
                pool_id=pool["pool_id"],
                amount=1,
            )

    def test_duplicate_occupation_is_rejected(self):
        pool = self.make_pool()
        app = self.make_app()
        self.make_issued(pool["pool_id"], app["application_id"], 100_000)
        # 同一申报在同一资源池再次下达 → 行被阻断，不产生任何扣减
        batch = self.svc.create_batch(
            actor="财政专员",
            year=2026,
            lines=[{"application_id": app["application_id"], "pool_id": pool["pool_id"], "amount": 50_000}],
        )
        self.svc.approve_batch(batch_id=batch["batch_id"], actor="教育部门")
        result = self.svc.issue_batch(batch_id=batch["batch_id"], actor="教育部门")
        self.assertEqual(result["status"], "failed")
        self.assertIn("重复占用", result["failed_line"]["reason"])
        self.assertEqual(self.svc.get_pool(pool["pool_id"])["locked_amount"], 100_000)

    def test_blocked_line_adjust_and_resume(self):
        pool = self.make_pool(budget_amount=100)
        app_a = self.make_app(applicant="甲大学")
        app_b = self.make_app(applicant="乙大学")
        batch = self.svc.create_batch(
            actor="财政专员",
            year=2026,
            lines=[
                {"application_id": app_a["application_id"], "pool_id": pool["pool_id"], "amount": 60},
                {"application_id": app_b["application_id"], "pool_id": pool["pool_id"], "amount": 60},
            ],
        )
        self.svc.approve_batch(batch_id=batch["batch_id"], actor="教育部门")
        result = self.svc.issue_batch(batch_id=batch["batch_id"], actor="教育部门")
        # 第二行额度不足：批次失败，第一行已入账不受影响
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_line"]["details"]["available_amount"], 40)
        self.assertEqual(self.svc.get_pool(pool["pool_id"])["locked_amount"], 60)
        # 调减留痕后续办：批次完成，账实一致
        line_id = result["failed_line"]["line_id"]
        adjust = self.svc.adjust_line(
            batch_id=batch["batch_id"],
            line_id=line_id,
            new_amount=40,
            reason="可用额度仅 40，按剩余额度调减",
            actor="财政专员",
        )
        self.assertEqual(adjust["old_amount"], 60)
        resumed = self.svc.issue_batch(batch_id=batch["batch_id"], actor="教育部门")
        self.assertEqual(resumed["status"], "issued")
        self.assertEqual(self.svc.get_pool(pool["pool_id"])["locked_amount"], 100)
        self.assertTrue(self.svc.reconcile()["consistent"])

    # ------------------------------------------------------------------
    # 退回与部分释放
    # ------------------------------------------------------------------
    def test_return_and_partial_release_keep_books_consistent(self):
        pool = self.make_pool()
        app = self.make_app()
        self.make_issued(pool["pool_id"], app["application_id"], 500)
        holding = self.svc.get_application(app["application_id"])["holdings"][0]
        # 部分释放 200
        partial = self.svc.release_holding(
            holding_id=holding["holding_id"], amount=200, reason="阶段调减", actor="财政专员"
        )
        self.assertEqual(partial["remaining_amount"], 300)
        self.assertEqual(self.svc.get_pool(pool["pool_id"])["locked_amount"], 300)
        self.assertEqual(
            self.svc.get_application(app["application_id"])["status"], "partially_returned"
        )
        # 退回剩余全部
        returned = self.svc.return_application(
            application_id=app["application_id"], reason="项目终止", actor="教育部门"
        )
        self.assertEqual(returned["status"], "returned")
        self.assertEqual(self.svc.get_pool(pool["pool_id"])["locked_amount"], 0)
        self.assertTrue(self.svc.reconcile()["consistent"])
        # 已退回不能重复退回
        with self.assertRaises(StateError):
            self.svc.return_application(
                application_id=app["application_id"], reason="重复退回", actor="教育部门"
            )

    def test_request_id_replay_is_safe(self):
        pool = self.make_pool()
        app = self.make_app()
        self.make_issued(pool["pool_id"], app["application_id"], 500)
        holding = self.svc.get_application(app["application_id"])["holdings"][0]
        first = self.svc.release_holding(
            holding_id=holding["holding_id"],
            amount=100,
            reason="调减",
            actor="财政专员",
            request_id="req-0001",
        )
        second = self.svc.release_holding(
            holding_id=holding["holding_id"],
            amount=100,
            reason="调减",
            actor="财政专员",
            request_id="req-0001",
        )
        self.assertNotIn("replayed", first)
        self.assertTrue(second["replayed"])
        self.assertEqual(self.svc.get_pool(pool["pool_id"])["locked_amount"], 400)
        entries = [
            e for e in self.svc.pool_ledger(pool["pool_id"]) if e["action"] == "release"
        ]
        self.assertEqual(len(entries), 1)  # 重试没有产生第二笔释放

    # ------------------------------------------------------------------
    # 跨项目调剂
    # ------------------------------------------------------------------
    def test_reallocation_same_pool_and_cross_pool_with_floor(self):
        pool1 = self.make_pool(budget_amount=1_000, floor_ratio_bp=3000)  # 保底 300
        pool2 = self.make_pool(tracks=["人文"], budget_amount=500, floor_ratio_bp=0)
        app_a = self.make_app(applicant="甲大学")
        app_b = self.make_app(applicant="乙大学")
        app_c = self.make_app(track="人文", applicant="丙大学")
        self.make_issued(pool1["pool_id"], app_a["application_id"], 400)
        holding_a = self.svc.get_application(app_a["application_id"])["holdings"][0]
        # 同池调剂：池锁定不变，占用在项目间转移
        same = self.svc.reallocate(
            from_holding_id=holding_a["holding_id"],
            to_application_id=app_b["application_id"],
            amount=150,
            reason="项目间调剂",
            actor="教育部门",
        )
        self.assertEqual(same["from"]["remaining_amount"], 250)
        self.assertEqual(self.svc.get_pool(pool1["pool_id"])["locked_amount"], 400)
        # 跨池调剂：源池锁定 400 -> 300，恰好在保底线上
        cross = self.svc.reallocate(
            from_holding_id=holding_a["holding_id"],
            to_application_id=app_c["application_id"],
            to_pool_id=pool2["pool_id"],
            amount=100,
            reason="跨赛道支援",
            actor="财政专员",
        )
        self.assertEqual(cross["to"]["pool_id"], pool2["pool_id"])
        self.assertEqual(self.svc.get_pool(pool1["pool_id"])["locked_amount"], 300)
        self.assertEqual(self.svc.get_pool(pool2["pool_id"])["locked_amount"], 100)
        # 再调 1 元就跌破保底：报错中带可解释依据
        with self.assertRaises(QuotaError) as ctx:
            self.svc.reallocate(
                from_holding_id=holding_a["holding_id"],
                to_application_id=app_c["application_id"],
                to_pool_id=pool2["pool_id"],
                amount=1,
                reason="试图突破保底",
                actor="财政专员",
            )
        self.assertEqual(ctx.exception.details["floor_amount"], 300)
        self.assertEqual(ctx.exception.details["max_transferable"], 0)
        self.assertTrue(self.svc.reconcile()["consistent"])

    def test_reallocation_track_mismatch_rejected(self):
        pool = self.make_pool()
        app_a = self.make_app(applicant="甲大学")
        app_med = self.make_app(track="医学", applicant="丙大学")
        self.make_issued(pool["pool_id"], app_a["application_id"], 100)
        holding = self.svc.get_application(app_a["application_id"])["holdings"][0]
        with self.assertRaises(QuotaError):
            self.svc.reallocate(
                from_holding_id=holding["holding_id"],
                to_application_id=app_med["application_id"],
                amount=10,
                reason="赛道不符",
                actor="教育部门",
            )

    # ------------------------------------------------------------------
    # 恢复续办
    # ------------------------------------------------------------------
    def test_recovery_resumes_without_double_deduction(self):
        pool = self.make_pool()
        apps = [self.make_app(applicant=f"高校{i}") for i in "ABC"]
        batch = self.svc.create_batch(
            actor="财政专员",
            year=2026,
            lines=[
                {"application_id": apps[0]["application_id"], "pool_id": pool["pool_id"], "amount": 100},
                {"application_id": apps[1]["application_id"], "pool_id": pool["pool_id"], "amount": 200},
                {"application_id": apps[2]["application_id"], "pool_id": pool["pool_id"], "amount": 300},
            ],
        )
        self.svc.approve_batch(batch_id=batch["batch_id"], actor="教育部门")
        # 模拟崩溃：两行入账后进程退出，批次停在"下达中"
        original = self.svc._apply_line
        calls = {"n": 0}

        def flaky(conn, batch_id, line_id, actor):
            if calls["n"] >= 2:
                raise RuntimeError("模拟进程崩溃")
            calls["n"] += 1
            return original(conn, batch_id, line_id, actor)

        self.svc._apply_line = flaky
        with self.assertRaises(RuntimeError):
            self.svc.issue_batch(batch_id=batch["batch_id"], actor="教育部门")
        self.assertEqual(self.svc.get_batch(batch["batch_id"])["status"], "issuing")
        self.assertEqual(self.svc.get_pool(pool["pool_id"])["locked_amount"], 300)
        # 服务重启：新实例恢复续办，只补未完成的行
        restarted = AllocationService(self.db_path)
        report = restarted.recover()
        self.assertEqual(report["resumed_count"], 1)
        self.assertEqual(report["batches"][0]["status"], "issued")
        self.assertEqual(restarted.get_pool(pool["pool_id"])["locked_amount"], 600)
        locks = [
            e for e in restarted.pool_ledger(pool["pool_id"]) if e["action"] == "lock"
        ]
        self.assertEqual(len(locks), 3)  # 没有重复扣减
        # 再次恢复/重复下达都是安全空转
        self.assertEqual(restarted.recover()["resumed_count"], 0)
        again = restarted.issue_batch(batch_id=batch["batch_id"], actor="教育部门")
        self.assertEqual(again["status"], "issued")
        self.assertEqual(restarted.get_pool(pool["pool_id"])["locked_amount"], 600)
        self.assertTrue(restarted.reconcile()["consistent"])

    # ------------------------------------------------------------------
    # 限制条件
    # ------------------------------------------------------------------
    def test_restrictions_are_enforced(self):
        pool = self.make_pool(
            restrictions={
                "max_single_amount": 100,
                "per_applicant_cap": 150,
                "allowed_applicants": ["甲大学", "乙大学"],
            }
        )
        app = self.make_app(applicant="甲大学")
        outsider = self.make_app(applicant="局外大学")
        # 单项上限
        batch = self.svc.create_batch(
            actor="财政专员",
            year=2026,
            lines=[{"application_id": app["application_id"], "pool_id": pool["pool_id"], "amount": 120}],
        )
        self.svc.approve_batch(batch_id=batch["batch_id"], actor="教育部门")
        result = self.svc.issue_batch(batch_id=batch["batch_id"], actor="教育部门")
        self.assertEqual(result["status"], "failed")
        self.assertIn("单项上限", result["failed_line"]["reason"])
        # 允许名单
        batch2 = self.svc.create_batch(
            actor="财政专员",
            year=2026,
            lines=[{"application_id": outsider["application_id"], "pool_id": pool["pool_id"], "amount": 10}],
        )
        self.svc.approve_batch(batch_id=batch2["batch_id"], actor="教育部门")
        result2 = self.svc.issue_batch(batch_id=batch2["batch_id"], actor="教育部门")
        self.assertEqual(result2["status"], "failed")
        self.assertIn("允许名单", result2["failed_line"]["reason"])
        # 在账上限：先下 100，再下 60 触发 150 上限
        self.make_issued(pool["pool_id"], app["application_id"], 100)
        app_b = self.make_app(applicant="甲大学", title="二期")
        batch3 = self.svc.create_batch(
            actor="财政专员",
            year=2026,
            lines=[{"application_id": app_b["application_id"], "pool_id": pool["pool_id"], "amount": 60}],
        )
        self.svc.approve_batch(batch_id=batch3["batch_id"], actor="教育部门")
        result3 = self.svc.issue_batch(batch_id=batch3["batch_id"], actor="教育部门")
        self.assertEqual(result3["status"], "failed")
        self.assertIn("在账上限", result3["failed_line"]["reason"])
        self.assertEqual(self.svc.get_pool(pool["pool_id"])["locked_amount"], 100)

    # ------------------------------------------------------------------
    # 预算修订与混合操作对账
    # ------------------------------------------------------------------
    def test_budget_revision_keeps_audit(self):
        pool = self.make_pool()
        app = self.make_app()
        self.make_issued(pool["pool_id"], app["application_id"], 400_000)
        with self.assertRaises(QuotaError):
            self.svc.revise_budget(
                pool_id=pool["pool_id"],
                new_budget=300_000,
                reason="低于已锁定额度",
                actor="财政专员",
            )
        revised = self.svc.revise_budget(
            pool_id=pool["pool_id"],
            new_budget=800_000,
            reason="年度调减，已锁定部分保留",
            actor="财政专员",
        )
        self.assertEqual(revised["budget_amount"], 800_000)
        self.assertEqual(revised["floor_amount"], 160_000)
        self.assertEqual(revised["available_amount"], 400_000)

    def test_reconcile_after_mixed_operations(self):
        pool1 = self.make_pool(floor_ratio_bp=1000)  # 保底 10 万
        pool2 = self.make_pool(tracks=["理工", "人文"], budget_amount=200_000)
        app_a = self.make_app(applicant="甲大学")
        app_b = self.make_app(applicant="乙大学")
        app_c = self.make_app(track="人文", applicant="丙大学")
        self.make_issued(pool1["pool_id"], app_a["application_id"], 300_000)
        self.make_issued(pool2["pool_id"], app_c["application_id"], 50_000)
        holding_a = self.svc.get_application(app_a["application_id"])["holdings"][0]
        self.svc.release_holding(
            holding_id=holding_a["holding_id"], amount=50_000, reason="部分释放", actor="财政专员"
        )
        self.svc.reallocate(
            from_holding_id=holding_a["holding_id"],
            to_application_id=app_b["application_id"],
            amount=100_000,
            reason="同池调剂",
            actor="教育部门",
        )
        self.svc.reallocate(
            from_holding_id=holding_a["holding_id"],
            to_application_id=app_c["application_id"],
            to_pool_id=pool2["pool_id"],
            amount=100_000,
            reason="跨池调剂",
            actor="财政专员",
        )
        report = self.svc.reconcile()
        self.assertTrue(report["consistent"])
        # pool1：30万 - 5万释放 - 10万跨池调出 = 15万（同池调剂不动池锁定）
        self.assertEqual(self.svc.get_pool(pool1["pool_id"])["locked_amount"], 150_000)
        # pool2：5万 + 10万调入 = 15万
        self.assertEqual(self.svc.get_pool(pool2["pool_id"])["locked_amount"], 150_000)


if __name__ == "__main__":
    unittest.main()
