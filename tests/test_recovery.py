"""崩溃恢复：未完成批次续办且不重复扣减；不可变台账防篡改。"""
from __future__ import annotations

from _service_case import ServiceCase
from resource_allocation.errors import QuotaExceededError
from resource_allocation.models import BatchStatus, TxnType
from resource_allocation import ledger


class CrashRecoveryTest(ServiceCase):
    def _approved_multi_item_plan(self, amounts=(2_000_000, 3_000_000)):
        """多资源类型方案（资金 + 师资名额）。"""
        self.svc.create_account(
            year=2026, kind="fund", name="资金账", total_amount=10_000_000,
            tracks=[{"track": "AI", "guarantee_bps": 0}],
        )
        self.svc.create_account(
            year=2026, kind="faculty", name="师资账", total_amount=100,
            tracks=[{"track": "AI", "guarantee_bps": 0}],
        )
        self.svc.create_project(code="P-AI", name="高校AI项目", track="AI")
        plan = self.svc.create_plan(
            project_code="P-AI", year=2026, title="多资源方案",
            items=[
                {"kind": "fund", "amount": amounts[0], "account": "资金账"},
                {"kind": "faculty", "amount": amounts[1] // 1_000_000,
                 "account": "师资账"},
            ],
            idempotency_key="mp",
        )
        self.svc.submit_plan(plan["id"])
        self.svc.approve_plan(plan["id"])
        return plan

    def test_recover_pending_batch(self) -> None:
        """批次已登记但从未执行（崩溃在登记与执行之间）：recover 完成下达。"""
        plan = self._approved_multi_item_plan()
        # 手工登记 pending 批次，模拟"意图已持久化、执行尚未开始"
        self.conn.execute(
            "INSERT INTO batches(idem_key, plan_id, action, reason, status, created_at)"
            " VALUES (?,?,?,?,?,?)",
            ("crash-1", plan["id"], "issue", "", "pending", ledger.utcnow()),
        )
        batch_id = 1
        items = self.conn.execute(
            "SELECT kind, amount, account_name FROM plan_items WHERE plan_id=?",
            (plan["id"],),
        ).fetchall()
        for seq, it in enumerate(items):
            self.conn.execute(
                "INSERT INTO batch_items(batch_id, seq, kind, amount, "
                "account_name, status) VALUES (?,?,?,?,?,?)",
                (batch_id, seq, it["kind"], it["amount"], it["account_name"], "pending"),
            )
        self.conn.commit()

        report = self.svc.recover()
        self.assertEqual(len(report["resumed"]), 1)
        self.assertEqual(self.svc.get_batch(batch_id)["status"], "done")
        self.assertEqual(self.svc.get_plan(plan["id"])["status"], "issued")
        allocs = self.svc.list_allocations(plan["id"])
        self.assertEqual(len(allocs), 2)
        self.assertTrue(all(a["outstanding"] == a["amount"] for a in allocs))

    def test_recover_running_batch_after_rollback(self) -> None:
        """执行中途崩溃：事务整体回滚，批次停留 running；recover 续办不重复扣。"""
        plan = self._approved_multi_item_plan()

        calls = {"n": 0}
        real_append = ledger.append_entry

        def flaky(conn, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("模拟进程被 kill / IO 错误")
            return real_append(conn, **kwargs)

        # 登记后执行即崩溃
        ledger.append_entry = flaky  # type: ignore[assignment]
        try:
            with self.assertRaises(RuntimeError):
                self.svc.issue_plan(plan["id"], idem_key="crash-2")
        finally:
            ledger.append_entry = real_append  # type: ignore[assignment]

        # 崩溃现场：批次 running，但事务回滚、无任何台账/清单残留
        batch = self.conn.execute(
            "SELECT * FROM batches WHERE idem_key='crash-2'"
        ).fetchone()
        self.assertEqual(batch["status"], BatchStatus.RUNNING.value)
        self.assertEqual(len(self.svc.list_ledger_entries()), 0)
        self.assertEqual(self.svc.list_allocations(plan["id"]), [])

        # 服务恢复：续办成功
        self.svc.recover()
        self.assertEqual(self.svc.get_plan(plan["id"])["status"], "issued")
        entries = self.svc.list_ledger_entries()
        reserves = [e for e in entries if e["txn_type"] == TxnType.RESERVE.value]
        self.assertEqual(len(reserves), 2)  # 没有重复扣减
        self.assertEqual(len(self.svc.list_allocations(plan["id"])), 2)

        # 再次 recover 幂等：已完成的批次不会被重放
        again = self.svc.recover()
        self.assertEqual(again["resumed"], [])
        self.assertEqual(
            len([e for e in self.svc.list_ledger_entries()
                 if e["txn_type"] == TxnType.RESERVE.value]),
            2,
        )

    def test_recover_release_batch(self) -> None:
        """部分释放批次中断后的续办。"""
        plan = self._approved_multi_item_plan()
        self.svc.issue_plan(plan["id"], idem_key="ok-issue")
        aid = next(
            a["id"] for a in self.svc.list_allocations(plan["id"])
            if a["kind"] == "fund"
        )
        cur = self.conn.execute(
            "INSERT INTO batches(idem_key, plan_id, action, reason, status, created_at)"
            " VALUES (?,?,?,?,?,?)",
            ("crash-rel", plan["id"], "release", "核减依据", "pending",
             ledger.utcnow()),
        )
        rel_batch = cur.lastrowid
        self.conn.execute(
            "INSERT INTO batch_items(batch_id, seq, allocation_id, kind, amount, "
            "mode, status) VALUES (?,0,?,'fund',100,'release','pending')",
            (rel_batch, aid),
        )
        self.conn.commit()
        self.svc.recover()
        self.assertEqual(self.svc.get_batch(rel_batch)["status"], "done")
        alloc = self.svc.list_allocations(plan["id"])[0]
        self.assertEqual(alloc["outstanding"], alloc["amount"] - 100)

    def test_recover_pending_transfer(self) -> None:
        """调剂单登记后崩溃：recover 原子完成划出+划入。"""
        self.svc.create_account(
            year=2026, kind="fund", name="资金账", total_amount=10_000_000,
            tracks=[{"track": "AI", "guarantee_bps": 0}],
        )
        self.svc.create_project(code="P-AI", name="A", track="AI")
        self.svc.create_project(code="P-C", name="C", track="AI")
        src = self.svc.create_plan(
            project_code="P-AI", year=2026, title="源",
            items=[{"kind": "fund", "amount": 3_000_000, "account": "资金账"}],
            idempotency_key="src",
        )
        self.svc.submit_plan(src["id"])
        self.svc.approve_plan(src["id"])
        self.svc.issue_plan(src["id"], idem_key="isrc")
        dst = self.svc.create_plan(
            project_code="P-C", year=2026, title="接收", items=[],
            idempotency_key="dst",
        )
        self.svc.submit_plan(dst["id"])
        self.svc.approve_plan(dst["id"])
        aid = self.svc.list_allocations(src["id"])[0]["id"]

        self.conn.execute(
            "INSERT INTO transfers(idem_key, from_plan_id, to_plan_id, status, "
            "reason, created_at) VALUES (?,?,?,?,?,?)",
            ("tr-crash", src["id"], dst["id"], "pending", "统筹", ledger.utcnow()),
        )
        self.conn.execute(
            "INSERT INTO transfer_items(transfer_id, seq, from_allocation_id, amount)"
            " VALUES (1,0,?,?)",
            (aid, 1_000_000),
        )
        self.conn.commit()

        self.svc.recover()
        self.assertEqual(self.svc.get_transfer(1)["status"], "done")
        self.assertEqual(
            self.svc.list_allocations(src["id"])[0]["outstanding"], 2_000_000
        )
        self.assertEqual(len(self.svc.list_allocations(dst["id"])), 1)

    def test_recover_fails_when_quota_gone(self) -> None:
        """停机期间额度被他人合法占用：续办重验失败，批次转 failed 且不超额。"""
        plan = self._approved_multi_item_plan()
        # 注册一个需要 200 万资金的 pending 批次
        self.conn.execute(
            "INSERT INTO batches(idem_key, plan_id, action, reason, status, created_at)"
            " VALUES (?,?,?,?,?,?)",
            ("crash-x", plan["id"], "issue", "", "pending", ledger.utcnow()),
        )
        items = self.conn.execute(
            "SELECT kind, amount, account_name FROM plan_items WHERE plan_id=?",
            (plan["id"],),
        ).fetchall()
        for seq, it in enumerate(items):
            self.conn.execute(
                "INSERT INTO batch_items(batch_id, seq, kind, amount, "
                "account_name, status) VALUES (?,?,?,?,?,?)",
                (1, seq, it["kind"], it["amount"], it["account_name"], "pending"),
            )
        self.conn.commit()
        # 停机期间：另一个方案抢先下达，把资金额度占满
        other = self.svc.create_plan(
            project_code="P-AI", year=2026, title="插队",
            items=[{"kind": "fund", "amount": 9_000_000, "account": "资金账"}],
            idempotency_key="other",
        )
        self.svc.submit_plan(other["id"])
        self.svc.approve_plan(other["id"])
        self.svc.issue_plan(other["id"], idem_key="iother")

        self.svc.recover()
        self.assertEqual(self.svc.get_batch(1)["status"], "failed")
        self.assertEqual(self.svc.get_plan(plan["id"])["status"], "approved")
        self.assertTrue(self.svc.reconcile()["balanced"])


class ImmutableLedgerTest(ServiceCase):
    def setUp(self) -> None:
        super().setUp()
        self.svc.create_account(
            year=2026, kind="fund", name="资金账", total_amount=10_000_000,
            tracks=[{"track": "AI", "guarantee_bps": 0}],
        )
        self.svc.create_project(code="P-AI", name="A", track="AI")
        plan = self.svc.create_plan(
            project_code="P-AI", year=2026, title="p",
            items=[{"kind": "fund", "amount": 100, "account": "资金账"}],
            idempotency_key="k",
        )
        self.svc.submit_plan(plan["id"])
        self.svc.approve_plan(plan["id"])
        self.svc.issue_plan(plan["id"], idem_key="i")
        self.plan_id = plan["id"]

    def test_ledger_rows_cannot_update(self) -> None:
        import sqlite3

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE ledger_entries SET amount=1 WHERE id=1")

    def test_ledger_rows_cannot_delete(self) -> None:
        import sqlite3

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM ledger_entries WHERE id=1")

    def test_allocations_cannot_update_or_delete(self) -> None:
        import sqlite3

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE allocations SET amount=1 WHERE id=1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM allocations WHERE id=1")

    def test_hash_chain_detects_tampering(self) -> None:
        from resource_allocation.errors import LedgerIntegrityError

        # 正常通过
        self.svc.reconcile()
        # 绕过触发器（模拟直接改库文件）后，哈希链第二道防线必须发现
        self.conn.execute("DROP TRIGGER trg_ledger_no_update")
        self.conn.execute("UPDATE ledger_entries SET amount=999 WHERE id=1")
        with self.assertRaises(LedgerIntegrityError):
            self.svc.reconcile()

    def test_hash_chain_links_entries(self) -> None:
        aid = self.svc.list_allocations(self.plan_id)[0]["id"]
        self.svc.release_resources(
            self.plan_id,
            items=[{"allocation_id": aid, "amount": 40, "mode": "release"}],
            reason="核减", idem_key="r",
        )
        rows = self.svc.list_ledger_entries()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["prev_hash"], rows[0]["entry_hash"])
        self.assertNotEqual(rows[0]["entry_hash"], rows[1]["entry_hash"])
