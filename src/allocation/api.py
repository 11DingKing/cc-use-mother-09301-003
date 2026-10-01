"""HTTP 接口层：仅依赖标准库的 JSON 服务。

约定：

- 请求与响应均为 JSON；错误统一为 ``{"error": {"code", "message", "details"}}``；
- 变更类接口支持 ``request_id`` 字段或 ``Idempotency-Key`` 请求头做幂等重试；
- 服务启动时自动执行恢复续办（``recover``），结果体现在 ``GET /health``。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, NotFoundError, ValidationError
from .service import AllocationService

Handler = Callable[[object, dict, dict, dict], "tuple[int, dict]"]


def _h_health(ctx, params, body, query):
    return 200, {"status": "ok", "recovery": ctx.recovery_report}


def _h_create_pool(ctx, params, body, query):
    return 201, ctx.service.create_pool(**body)


def _h_list_pools(ctx, params, body, query):
    year = query.get("year")
    pools = ctx.service.list_pools(
        year=int(year) if year is not None else None,
        resource_type=query.get("resource_type"),
    )
    return 200, {"pools": pools}


def _h_get_pool(ctx, params, body, query):
    return 200, ctx.service.get_pool(params["pool_id"])


def _h_pool_holdings(ctx, params, body, query):
    return 200, {"holdings": ctx.service.pool_holdings(params["pool_id"])}


def _h_pool_ledger(ctx, params, body, query):
    return 200, {"entries": ctx.service.pool_ledger(params["pool_id"])}


def _h_revise_budget(ctx, params, body, query):
    return 200, ctx.service.revise_budget(pool_id=params["pool_id"], **body)


def _h_create_application(ctx, params, body, query):
    return 201, ctx.service.create_application(**body)


def _h_get_application(ctx, params, body, query):
    return 200, ctx.service.get_application(params["application_id"])


def _h_return_application(ctx, params, body, query):
    return 200, ctx.service.return_application(application_id=params["application_id"], **body)


def _h_create_plan(ctx, params, body, query):
    return 201, ctx.service.create_plan(**body)


def _h_get_plan(ctx, params, body, query):
    return 200, ctx.service.get_plan(params["plan_id"])


def _h_add_plan_item(ctx, params, body, query):
    return 200, ctx.service.add_plan_item(plan_id=params["plan_id"], **body)


def _h_simulate_plan(ctx, params, body, query):
    return 200, ctx.service.simulate_plan(params["plan_id"])


def _h_compare_plans(ctx, params, body, query):
    return 200, ctx.service.compare_plans(body.get("plan_ids"))


def _h_create_batch(ctx, params, body, query):
    return 201, ctx.service.create_batch(**body)


def _h_list_batches(ctx, params, body, query):
    return 200, {"batches": ctx.service.list_batches(status=query.get("status"))}


def _h_get_batch(ctx, params, body, query):
    return 200, ctx.service.get_batch(params["batch_id"])


def _h_approve_batch(ctx, params, body, query):
    return 200, ctx.service.approve_batch(batch_id=params["batch_id"], **body)


def _h_issue_batch(ctx, params, body, query):
    return 200, ctx.service.issue_batch(batch_id=params["batch_id"], **body)


def _h_batch_manifest(ctx, params, body, query):
    return 200, ctx.service.batch_manifest(params["batch_id"])


def _h_adjust_line(ctx, params, body, query):
    return 200, ctx.service.adjust_line(
        batch_id=params["batch_id"], line_id=params["line_id"], **body
    )


def _h_release_holding(ctx, params, body, query):
    return 200, ctx.service.release_holding(holding_id=params["holding_id"], **body)


def _h_reallocate(ctx, params, body, query):
    return 201, ctx.service.reallocate(**body)


def _h_reconcile(ctx, params, body, query):
    return 200, ctx.service.reconcile(pool_id=query.get("pool_id"))


def _h_recover(ctx, params, body, query):
    return 200, ctx.service.recover(actor=body.get("actor", "system"))


def _compile(pattern: str) -> re.Pattern:
    return re.compile(re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern))


# 注意顺序：/plans/compare 必须排在 /plans/{plan_id} 之前
ROUTES = [
    ("GET", _compile("/health"), _h_health),
    ("POST", _compile("/pools"), _h_create_pool),
    ("GET", _compile("/pools"), _h_list_pools),
    ("GET", _compile("/pools/{pool_id}"), _h_get_pool),
    ("GET", _compile("/pools/{pool_id}/holdings"), _h_pool_holdings),
    ("GET", _compile("/pools/{pool_id}/ledger"), _h_pool_ledger),
    ("POST", _compile("/pools/{pool_id}/budget"), _h_revise_budget),
    ("POST", _compile("/applications"), _h_create_application),
    ("GET", _compile("/applications/{application_id}"), _h_get_application),
    ("POST", _compile("/applications/{application_id}/return"), _h_return_application),
    ("POST", _compile("/plans"), _h_create_plan),
    ("POST", _compile("/plans/compare"), _h_compare_plans),
    ("GET", _compile("/plans/{plan_id}"), _h_get_plan),
    ("POST", _compile("/plans/{plan_id}/items"), _h_add_plan_item),
    ("GET", _compile("/plans/{plan_id}/simulate"), _h_simulate_plan),
    ("POST", _compile("/batches"), _h_create_batch),
    ("GET", _compile("/batches"), _h_list_batches),
    ("GET", _compile("/batches/{batch_id}"), _h_get_batch),
    ("POST", _compile("/batches/{batch_id}/approve"), _h_approve_batch),
    ("POST", _compile("/batches/{batch_id}/issue"), _h_issue_batch),
    ("GET", _compile("/batches/{batch_id}/manifest"), _h_batch_manifest),
    ("POST", _compile("/batches/{batch_id}/lines/{line_id}/adjust"), _h_adjust_line),
    ("POST", _compile("/holdings/{holding_id}/release"), _h_release_holding),
    ("POST", _compile("/reallocations"), _h_reallocate),
    ("GET", _compile("/reconcile"), _h_reconcile),
    ("POST", _compile("/admin/recover"), _h_recover),
]


class _RequestHandler(BaseHTTPRequestHandler):
    server_version = "Allocation/0.1"

    def do_GET(self) -> None:  # noqa: N802 - 标准库约定
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - 标准库约定
        self._dispatch("POST")

    def log_message(self, format: str, *args) -> None:  # 静默访问日志
        return

    def _dispatch(self, method: str) -> None:
        try:
            status, payload = self._route(method)
        except DomainError as exc:
            status, payload = exc.http_status, {"error": exc.to_dict()}
        except json.JSONDecodeError as exc:
            status, payload = 400, {
                "error": {
                    "code": "invalid_json",
                    "message": f"请求体不是合法 JSON：{exc}",
                    "details": {},
                }
            }
        except (TypeError, ValueError) as exc:
            status, payload = 400, {
                "error": {"code": "bad_request", "message": str(exc), "details": {}}
            }
        except Exception as exc:  # noqa: BLE001 - 兜底，避免连接被静默重置
            status, payload = 500, {
                "error": {"code": "internal_error", "message": str(exc), "details": {}}
            }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _route(self, method: str) -> "tuple[int, dict]":
        parsed = urlparse(self.path)
        body: dict = {}
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValidationError("请求体必须是 JSON 对象")
        key = self.headers.get("Idempotency-Key")
        if key and "request_id" not in body:
            body["request_id"] = key
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        for route_method, pattern, handler in ROUTES:
            if route_method != method:
                continue
            match = pattern.fullmatch(parsed.path)
            if match:
                return handler(self.server, match.groupdict(), body, query)
        raise NotFoundError(
            "接口不存在", details={"method": method, "path": parsed.path}
        )


def make_server(
    service: AllocationService, host: str = "127.0.0.1", port: int = 8080
) -> ThreadingHTTPServer:
    """构建 HTTP 服务；启动即执行恢复续办，未完成批次自动继续。"""
    recovery_report = service.recover()

    class _Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = _Server((host, port), _RequestHandler)
    server.service = service  # type: ignore[attr-defined]
    server.recovery_report = recovery_report  # type: ignore[attr-defined]
    return server


def run(host: str, port: int, db_path: str) -> None:
    service = AllocationService(db_path)
    server = make_server(service, host, port)
    actual_port = server.server_address[1]
    print(f"高校资源分类配置服务端已启动: http://{host}:{actual_port} (db={db_path})")
    if server.recovery_report["resumed_count"]:  # type: ignore[attr-defined]
        print(f"恢复续办：{server.recovery_report['resumed_count']} 个未完成批次已继续处理")  # type: ignore[attr-defined]
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
