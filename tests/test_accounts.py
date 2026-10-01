"""建账、赛道适用范围与保底比例校验。"""
from __future__ import annotations

from _service_case import ServiceCase
from resource_allocation.errors import (
    ConflictError,
    ConstraintViolationError,
    ValidationError,
)


class AccountTest(ServiceCase):
    def test_create_account_with_tracks(self) -> None:
        acct = self.setup_world()["account"]
        self.assertEqual(acct["total_amount"], 10_000_000)
        tracks = {t["track"]: t["guarantee_bps"] for t in acct["tracks"]}
        self.assertEqual(tracks, {"AI": 4000, "芯片": 4000})

    def test_duplicate_account_rejected(self) -> None:
        self.setup_world()
        with self.assertRaises(ConflictError):
            self.svc.create_account(
                year=2026,
                kind="fund",
                name="中央专项",
                total_amount=1,
                tracks=[{"track": "AI", "guarantee_bps": 0}],
            )

    def test_guarantee_sum_cannot_exceed_100pct(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.create_account(
                year=2026,
                kind="fund",
                name="X",
                total_amount=100,
                tracks=[
                    {"track": "AI", "guarantee_bps": 6000},
                    {"track": "芯片", "guarantee_bps": 5000},
                ],
            )

    def test_amount_must_be_positive_integer(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.create_account(
                year=2026, kind="fund", name="X", total_amount=0,
                tracks=[{"track": "AI", "guarantee_bps": 0}],
            )
        with self.assertRaises(ValidationError):
            self.svc.create_account(
                year=2026, kind="fund", name="Y", total_amount=10.5,  # type: ignore[arg-type]
                tracks=[{"track": "AI", "guarantee_bps": 0}],
            )

    def test_snapshot_starts_empty(self) -> None:
        acct = self.setup_world()["account"]
        snap = self.svc.account_snapshot(acct["id"])
        self.assertEqual(snap["used_amount"], 0)
        self.assertEqual(snap["available_amount"], 10_000_000)


class ProjectTrackTest(ServiceCase):
    def test_project_track_must_be_applicable(self) -> None:
        self.svc.create_account(
            year=2026, kind="fund", name="A", total_amount=10_000_000,
            tracks=[{"track": "AI", "guarantee_bps": 0}],
        )
        proj = self.svc.create_project(code="P1", name="生物项目", track="生物")
        plan = self.svc.create_plan(
            project_code="P1", year=2026, title="p",
            items=[{"kind": "fund", "amount": 100}], idempotency_key="k",
        )
        preview = self.svc.preview_plan(plan["id"])
        self.assertFalse(preview["feasible"])
        self.svc.submit_plan(plan["id"])
        self.svc.approve_plan(plan["id"])
        with self.assertRaises(ConstraintViolationError):
            self.svc.issue_plan(plan["id"], idem_key="i")
        # 失败下达不产生任何台账变动
        self.assertEqual(self.svc.account_snapshot(1)["used_amount"], 0)
        self.assertEqual(proj["track"], "生物")

    def test_max_per_project_enforced(self) -> None:
        world = self.setup_world()
        over = self.svc.create_plan(
            project_code="P-AI", year=2026, title="超上限",
            items=[{"kind": "fund", "amount": 9_000_000}], idempotency_key="k",
        )
        preview = self.svc.preview_plan(over["id"])
        self.assertFalse(preview["feasible"])
        self.svc.submit_plan(over["id"])
        self.svc.approve_plan(over["id"])
        from resource_allocation.errors import QuotaExceededError

        with self.assertRaises(QuotaExceededError):
            self.svc.issue_plan(over["id"], idem_key="i")
        self.assertEqual(
            self.svc.account_snapshot(world["account"]["id"])["used_amount"], 0
        )
