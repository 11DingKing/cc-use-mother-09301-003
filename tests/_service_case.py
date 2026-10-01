"""测试夹具：内存数据库与服务。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from resource_allocation.db import connect, init_db
from resource_allocation.service import ResourceService


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        init_db(self.conn)
        self.svc = ResourceService(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------- 建账帮手

    def setup_world(self, total: int = 10_000_000) -> dict:
        """典型场景：一个资金账户，AI/芯片两赛道各保底 40%。"""
        acct = self.svc.create_account(
            year=2026,
            kind="fund",
            name="中央专项",
            total_amount=total,
            max_per_project=8_000_000,
            tracks=[
                {"track": "AI", "guarantee_bps": 4000},
                {"track": "芯片", "guarantee_bps": 4000},
            ],
        )
        p_ai = self.svc.create_project(code="P-AI", name="高校AI项目", track="AI")
        p_chip = self.svc.create_project(code="P-CHIP", name="高校芯片项目", track="芯片")
        return {"account": acct, "ai": p_ai, "chip": p_chip}

    def make_issued_plan(
        self, code: str, project_code: str, amount: int, key: str
    ) -> dict:
        plan = self.svc.create_plan(
            project_code=project_code,
            year=2026,
            title=f"方案-{code}",
            items=[{"kind": "fund", "amount": amount}],
            idempotency_key=f"plan-{key}",
        )
        self.svc.submit_plan(plan["id"])
        self.svc.approve_plan(plan["id"])
        self.svc.issue_plan(plan["id"], idem_key=f"issue-{key}")
        return self.svc.get_plan(plan["id"])
