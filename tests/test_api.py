"""HTTP API 端到端测试：真实起服 + urllib 调用。"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from resource_allocation.api import make_handler, App  # noqa: E402


class ApiCase(unittest.TestCase):
    def setUp(self) -> None:
        self.app = App(":memory:")
        handler = make_handler(self.app)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.app.shutdown()

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        idem_key: str | None = None,
    ):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method
        )
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if idem_key:
            req.add_header("Idempotency-Key", idem_key)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_health(self) -> None:
        code, body = self.request("GET", "/healthz")
        self.assertEqual(code, 200)
        self.assertEqual(body, {"status": "ok"})

    def test_full_flow_over_http(self) -> None:
        code, acct = self.request("POST", "/api/accounts", {
            "year": 2026, "kind": "fund", "name": "专项",
            "total_amount": 10_000_000,
            "tracks": [
                {"track": "AI", "guarantee_bps": 3000},
                {"track": "芯片", "guarantee_bps": 3000},
            ],
        })
        self.assertEqual(code, 200, acct)

        code, _ = self.request("POST", "/api/projects",
                               {"code": "P1", "name": "A校", "track": "AI"})
        self.assertEqual(code, 200)

        code, plan = self.request("POST", "/api/plans", {
            "project_code": "P1", "year": 2026, "title": "方案",
            "items": [{"kind": "fund", "amount": 2_000_000}],
        }, idem_key="plan-create-0001")
        self.assertEqual(code, 200)
        plan_id = plan["id"]

        # 试算
        code, preview = self.request("POST", f"/api/plans/{plan_id}/preview")
        self.assertEqual(code, 200)
        self.assertTrue(preview["feasible"])

        code, _ = self.request("POST", f"/api/plans/{plan_id}/submit")
        self.assertEqual(code, 200)
        code, _ = self.request("POST", f"/api/plans/{plan_id}/approve")
        self.assertEqual(code, 200)

        # 下达：通过 Idempotency-Key 头幂等
        code, b1 = self.request("POST", f"/api/plans/{plan_id}/issue",
                                {}, idem_key="issue-0001")
        self.assertEqual(code, 200)
        self.assertEqual(b1["status"], "done")
        code, b2 = self.request("POST", f"/api/plans/{plan_id}/issue",
                                {}, idem_key="issue-0001")
        self.assertEqual(code, 200)
        self.assertEqual(b2["id"], b1["id"])

        code, snap = self.request("GET", f"/api/accounts/{acct['id']}/snapshot")
        self.assertEqual(code, 200)
        self.assertEqual(snap["used_amount"], 2_000_000)

        code, allocs = self.request("GET", f"/api/plans/{plan_id}/allocations")
        self.assertEqual(code, 200)
        self.assertEqual(len(allocs["allocations"]), 1)
        aid = allocs["allocations"][0]["id"]

        # 部分释放
        code, rel = self.request("POST", f"/api/plans/{plan_id}/release", {
            "items": [{"allocation_id": aid, "amount": 500_000, "mode": "release"}],
            "reason": "设备核减",
        }, idem_key="rel-0001")
        self.assertEqual(code, 200, rel)
        self.assertEqual(rel["status"], "done")

        # 对账
        code, rec = self.request("POST", "/api/reconcile")
        self.assertEqual(code, 200)
        self.assertTrue(rec["balanced"])
        self.assertEqual(rec["hash_chain"], "ok")

    def test_quota_exceeded_returns_422(self) -> None:
        self.request("POST", "/api/accounts", {
            "year": 2026, "kind": "fund", "name": "专项",
            "total_amount": 100,
            "tracks": [{"track": "AI", "guarantee_bps": 0}],
        })
        self.request("POST", "/api/projects",
                     {"code": "P1", "name": "A", "track": "AI"})
        _, plan = self.request("POST", "/api/plans", {
            "project_code": "P1", "year": 2026, "title": "p",
            "items": [{"kind": "fund", "amount": 999}],
        }, idem_key="pk")
        self.request("POST", f"/api/plans/{plan['id']}/submit")
        self.request("POST", f"/api/plans/{plan['id']}/approve")
        code, err = self.request("POST", f"/api/plans/{plan['id']}/issue",
                                 {}, idem_key="ik")
        self.assertEqual(code, 422)
        self.assertEqual(err["error"], "quota_exceeded")

    def test_missing_idempotency_key_rejected(self) -> None:
        code, err = self.request("POST", "/api/plans", {
            "project_code": "P1", "year": 2026, "title": "p",
            "items": [{"kind": "fund", "amount": 1}],
        })
        self.assertEqual(code, 400)
        self.assertEqual(err["error"], "domain_error")

    def test_unknown_route_404(self) -> None:
        code, err = self.request("GET", "/api/nope")
        self.assertEqual(code, 404)
        self.assertEqual(err["error"], "not_found")

    def test_bad_json_400(self) -> None:
        req = urllib.request.Request(
            self.base + "/api/accounts", data=b"{not json", method="POST"
        )
        req.add_header("Content-Type", "application/json")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
