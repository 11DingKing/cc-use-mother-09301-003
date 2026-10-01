"""试算隔离、保底可行性与方案生命周期。"""
from __future__ import annotations

from _service_case import ServiceCase
from resource_allocation.errors import InvalidStateError
from resource_allocation.models import PlanStatus


class TrialIsolationTest(ServiceCase):
    def test_drafts_do_not_touch_ledger(self) -> None:
        world = self.setup_world()
        for i in range(5):
            self.svc.create_plan(
                project_code="P-AI", year=2026, title=f"草案{i}",
                items=[{"kind": "fund", "amount": 9_000_000}],
                idempotency_key=f"draft-{i}",
            )
        # 任意数量的草案都不占额度，彼此互不影响
        snap = self.svc.account_snapshot(world["account"]["id"])
        self.assertEqual(snap["used_amount"], 0)
        self.assertEqual(len(self.svc.list_ledger_entries()), 0)

    def test_drafts_can_diverge(self) -> None:
        self.setup_world()
        a = self.svc.create_plan(
            project_code="P-AI", year=2026, title="方案A",
            items=[{"kind": "fund", "amount": 1_000_000}], idempotency_key="a",
        )
        b = self.svc.create_plan(
            project_code="P-AI", year=2026, title="方案B",
            items=[{"kind": "fund", "amount": 2_000_000}], idempotency_key="b",
        )
        self.assertEqual(a["items"][0]["amount"], 1_000_000)
        self.assertEqual(b["items"][0]["amount"], 2_000_000)
        # 修改草案不影响另一个
        self.svc.update_plan_items(
            a["id"], [{"kind": "fund", "amount": 1_500_000}]
        )
        self.assertEqual(self.svc.get_plan(a["id"])["items"][0]["amount"], 1_500_000)
        self.assertEqual(self.svc.get_plan(b["id"])["items"][0]["amount"], 2_000_000)

    def test_only_draft_editable(self) -> None:
        self.setup_world()
        plan = self.svc.create_plan(
            project_code="P-AI", year=2026, title="p",
            items=[{"kind": "fund", "amount": 100}], idempotency_key="k",
        )
        self.svc.submit_plan(plan["id"])
        with self.assertRaises(InvalidStateError):
            self.svc.update_plan_items(plan["id"], [{"kind": "fund", "amount": 200}])

    def test_create_plan_idempotent(self) -> None:
        self.setup_world()
        payload = dict(
            project_code="P-AI", year=2026, title="p",
            items=[{"kind": "fund", "amount": 100}], idempotency_key="dup",
        )
        a = self.svc.create_plan(**payload)
        b = self.svc.create_plan(**payload)
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(self.svc.list_plans()), 1)


class GuaranteeFloorTest(ServiceCase):
    def _account(self, total: int):
        acct = self.svc.create_account(
            year=2026, kind="fund", name="专项", total_amount=total,
            tracks=[
                {"track": "AI", "guarantee_bps": 4000},
                {"track": "芯片", "guarantee_bps": 4000},
            ],
        )
        self.svc.create_project(code="P-AI", name="AI校", track="AI")
        self.svc.create_project(code="P-CHIP", name="芯片校", track="芯片")
        return acct

    def test_floor_blocks_over_allocation_to_one_track(self) -> None:
        # AI 想拿 700 万：剩余 300 万 < 芯片保底 400 万，试算即提示不可行，
        # 下达时被拒绝
        acct = self._account(10_000_000)
        plan = self.svc.create_plan(
            project_code="P-AI", year=2026, title="过大",
            items=[{"kind": "fund", "amount": 7_000_000}], idempotency_key="k",
        )
        preview = self.svc.preview_plan(plan["id"])
        self.assertFalse(preview["feasible"])
        self.svc.submit_plan(plan["id"])
        self.svc.approve_plan(plan["id"])
        from resource_allocation.errors import QuotaExceededError

        with self.assertRaises(QuotaExceededError):
            self.svc.issue_plan(plan["id"], idem_key="i")
        self.assertEqual(self.svc.account_snapshot(acct["id"])["used_amount"], 0)

    def test_floor_allows_balanced_plans(self) -> None:
        acct = self._account(10_000_000)
        a = self.svc.create_plan(
            project_code="P-AI", year=2026, title="a",
            items=[{"kind": "fund", "amount": 4_000_000}], idempotency_key="a",
        )
        b = self.svc.create_plan(
            project_code="P-CHIP", year=2026, title="b",
            items=[{"kind": "fund", "amount": 4_000_000}], idempotency_key="b",
        )
        self.assertTrue(self.svc.preview_plan(a["id"])["feasible"])
        self.assertTrue(self.svc.preview_plan(b["id"])["feasible"])
        for p, k in ((a, "ia"), (b, "ib")):
            self.svc.submit_plan(p["id"])
            self.svc.approve_plan(p["id"])
            self.svc.issue_plan(p["id"], idem_key=k)
        snap = self.svc.account_snapshot(acct["id"])
        self.assertEqual(snap["used_amount"], 8_000_000)
        self.assertEqual(
            snap["used_by_track"], {"AI": 4_000_000, "芯片": 4_000_000}
        )

    def test_preview_warns_about_competing_approved_plans(self) -> None:
        self._account(10_000_000)
        a = self.svc.create_plan(
            project_code="P-AI", year=2026, title="a",
            items=[{"kind": "fund", "amount": 6_000_000}], idempotency_key="a",
        )
        self.svc.submit_plan(a["id"])
        self.svc.approve_plan(a["id"])
        b = self.svc.create_plan(
            project_code="P-CHIP", year=2026, title="b",
            items=[{"kind": "fund", "amount": 6_000_000}], idempotency_key="b",
        )
        # 试算时两案都显示"看起来可行"，但互相竞争；系统标注竞争方案
        preview = self.svc.preview_plan(b["id"])
        self.assertTrue(preview["feasible"])
        self.assertEqual(len(preview["competing_approved_plans"]), 1)


class LifecycleTest(ServiceCase):
    def test_submit_approve_flow(self) -> None:
        self.setup_world()
        plan = self.svc.create_plan(
            project_code="P-AI", year=2026, title="p",
            items=[{"kind": "fund", "amount": 100}], idempotency_key="k",
        )
        self.assertEqual(plan["status"], PlanStatus.DRAFT.value)
        self.svc.submit_plan(plan["id"])
        self.assertEqual(self.svc.get_plan(plan["id"])["status"], "submitted")
        self.svc.approve_plan(plan["id"])
        self.assertEqual(self.svc.get_plan(plan["id"])["status"], "approved")

    def test_reject_before_issue_keeps_quota_intact(self) -> None:
        world = self.setup_world()
        plan = self.svc.create_plan(
            project_code="P-AI", year=2026, title="p",
            items=[{"kind": "fund", "amount": 3_000_000}], idempotency_key="k",
        )
        self.svc.submit_plan(plan["id"])
        self.svc.reject_plan(plan["id"], reason="材料不全", idem_key="rj")
        self.assertEqual(self.svc.get_plan(plan["id"])["status"], "rejected")
        snap = self.svc.account_snapshot(world["account"]["id"])
        self.assertEqual(snap["used_amount"], 0)
        # 退回的方案可修改后重新报批
        self.svc.approve_plan(plan["id"])
        self.assertEqual(self.svc.get_plan(plan["id"])["status"], "approved")
