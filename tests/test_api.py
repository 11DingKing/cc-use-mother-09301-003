"""HTTP 接口冒烟测试：真实起服务、走完整业务流程。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from allocation import AllocationService, make_server


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        service = AllocationService(str(Path(cls.tmp.name) / "api.db"))
        cls.server = make_server(service, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method
        )
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, data=data, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_flow_over_http(self):
        # 建账
        status, pool = self.call(
            "POST",
            "/pools",
            {
                "year": 2026,
                "resource_type": "fund",
                "tracks": ["理工"],
                "unit": "元",
                "budget_amount": 1_000_000,
                "floor_ratio": 0.2,
                "restrictions": {"max_single_amount": 800_000},
                "actor": "财政专员",
            },
        )
        self.assertEqual(status, 201, pool)
        self.assertEqual(pool["floor_amount"], 200_000)
        # 申报
        status, app = self.call(
            "POST",
            "/applications",
            {
                "applicant": "甲大学",
                "track": "理工",
                "title": "重点实验室建设",
                "demands": [{"resource_type": "fund", "amount": 600_000}],
                "actor": "高校经办人",
            },
        )
        self.assertEqual(status, 201, app)
        # 试算（隔离）
        status, plan = self.call(
            "POST", "/plans", {"name": "方案A", "year": 2026, "actor": "教育部门"}
        )
        self.assertEqual(status, 201, plan)
        status, _ = self.call(
            "POST",
            f"/plans/{plan['plan_id']}/items",
            {
                "application_id": app["application_id"],
                "pool_id": pool["pool_id"],
                "amount": 600_000,
                "actor": "教育部门",
            },
        )
        self.assertEqual(status, 200)
        status, sim = self.call("GET", f"/plans/{plan['plan_id']}/simulate")
        self.assertTrue(sim["feasible"])
        status, real_pool = self.call("GET", f"/pools/{pool['pool_id']}")
        self.assertEqual(real_pool["locked_amount"], 0)  # 试算不占真账
        # 审批 → 下达
        status, batch = self.call(
            "POST", "/batches", {"plan_id": plan["plan_id"], "actor": "财政专员"}
        )
        self.assertEqual(status, 201, batch)
        status, _ = self.call(
            "POST", f"/batches/{batch['batch_id']}/approve", {"actor": "教育部门"}
        )
        self.assertEqual(status, 200)
        status, issued = self.call(
            "POST", f"/batches/{batch['batch_id']}/issue", {"actor": "教育部门"}
        )
        self.assertEqual(issued["status"], "issued")
        # 不可变清单
        status, manifest = self.call("GET", f"/batches/{batch['batch_id']}/manifest")
        self.assertTrue(manifest["complete"])
        self.assertEqual(manifest["lines"][0]["amount"], 600_000)
        self.assertEqual(len(manifest["digest"]), 64)
        # 部分释放（幂等键重试安全）
        status, app_view = self.call("GET", f"/applications/{app['application_id']}")
        holding_id = app_view["holdings"][0]["holding_id"]
        body = {"amount": 100_000, "reason": "阶段调减", "actor": "财政专员"}
        status, first = self.call(
            "POST",
            f"/holdings/{holding_id}/release",
            body,
            headers={"Idempotency-Key": "http-req-1"},
        )
        self.assertEqual(status, 200, first)
        status, replay = self.call(
            "POST",
            f"/holdings/{holding_id}/release",
            body,
            headers={"Idempotency-Key": "http-req-1"},
        )
        self.assertTrue(replay["replayed"])
        status, real_pool = self.call("GET", f"/pools/{pool['pool_id']}")
        self.assertEqual(real_pool["locked_amount"], 500_000)  # 只释放了一次
        # 对账
        status, report = self.call("GET", "/reconcile")
        self.assertTrue(report["consistent"])

    def test_error_shapes(self):
        status, payload = self.call("GET", "/pools/POOL-9999")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")
        status, payload = self.call("POST", "/pools", {"year": "abc"})
        self.assertEqual(status, 400)
        self.assertIn("error", payload)
        status, payload = self.call("GET", "/no-such-endpoint")
        self.assertEqual(status, 404)

    def test_recover_endpoint_and_health(self):
        status, health = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["status"], "ok")
        status, report = self.call("POST", "/admin/recover", {"actor": "system"})
        self.assertEqual(status, 200)
        self.assertIn("resumed_count", report)


if __name__ == "__main__":
    unittest.main()
