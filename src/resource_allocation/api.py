"""HTTP API（标准库 http.server，零第三方依赖）。

写操作通过 ``Idempotency-Key`` 头或请求体 ``idempotency_key`` 实现幂等：
网络重试、客户端超时重发不会产生第二笔扣减。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from .db import connect, init_db
from .errors import DomainError, NotFoundError
from .service import ResourceService

log = logging.getLogger("resource_allocation.api")

_IDEM_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class App:
    """持有数据库连接的应用上下文。"""

    def __init__(self, db_path: str = ":memory:") -> None:
        # check_same_thread=False：ThreadingHTTPServer 多线程共享；
        # 所有写操作走 BEGIN IMMEDIATE 串行化，读操作也在连接锁内进行。
        self.conn = connect(db_path, cross_thread=True)
        init_db(self.conn)
        self.lock = threading.RLock()
        self.service = ResourceService(self.conn)

    def shutdown(self) -> None:
        self.conn.close()


def _json_response(handler: BaseHTTPRequestHandler, code: int, body: Any) -> None:
    data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


# 路由表：(方法正则, 路径正则) -> 处理函数(app, m, body)
def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    service = app.service

    class Handler(BaseHTTPRequestHandler):
        server_version = "ResourceAllocation/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.info("%s - %s", self.address_string(), fmt % args)

        def _read_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise DomainError(f"请求体不是合法 JSON：{exc}")
            if not isinstance(body, dict):
                raise DomainError("请求体必须是 JSON 对象")
            return body

        def _idem_key(self, body: dict[str, Any]) -> str:
            key = self.headers.get("Idempotency-Key") or body.get("idempotency_key")
            if not key:
                raise DomainError("写操作必须提供 Idempotency-Key 头或 idempotency_key 字段")
            if not _IDEM_RE.match(str(key)):
                raise DomainError("幂等键只能含字母数字 . _ : -，长度不超过 128")
            return str(key)

        def _handle(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parsed.query
            try:
                with app.lock:
                    body = self._read_body() if method in ("POST", "PUT", "PATCH") else {}
                    resp = self._route(method, path, query, body)
                _json_response(self, 200, resp)
            except DomainError as exc:
                _json_response(self, exc.status_code, exc.to_dict())
            except TypeError as exc:
                _json_response(
                    self, 422,
                    {"error": "validation_error", "message": f"请求参数不匹配：{exc}"},
                )
            except Exception as exc:  # noqa: BLE001 - 兜底，避免连接被静默吞掉
                log.exception("未处理异常")
                _json_response(
                    self, 500, {"error": "internal_error", "message": str(exc)}
                )

        def _route(
            self, method: str, path: str, query: str, body: dict[str, Any]
        ) -> Any:
            from urllib.parse import parse_qs

            q = {k: v[0] for k, v in parse_qs(query).items()}
            p = path.split("/")

            def need_id(name: str) -> int:
                try:
                    return int(name)
                except (TypeError, ValueError):
                    raise NotFoundError(f"路径中的 id 必须是整数：{name}")

            # ---- 账户 ----
            if method == "POST" and path == "/api/accounts":
                return service.create_account(**body)
            if method == "GET" and path == "/api/accounts":
                return {"accounts": service.list_accounts(int(q["year"]) if "year" in q else None)}
            if method == "GET" and len(p) == 4 and p[1] == "api" and p[2] == "accounts":
                return service.get_account(need_id(p[3]))
            if method == "GET" and len(p) == 5 and p[1] == "api" and p[2] == "accounts" and p[4] == "snapshot":
                return service.account_snapshot(need_id(p[3]))

            # ---- 项目 ----
            if method == "POST" and path == "/api/projects":
                return service.create_project(**body)
            if method == "GET" and path == "/api/projects":
                return {"projects": [service.get_project(r["id"]) for r in
                                     service.conn.execute("SELECT id FROM projects ORDER BY id")]}
            if method == "GET" and len(p) == 4 and p[1] == "api" and p[2] == "projects":
                return service.get_project(need_id(p[3]))

            # ---- 方案 ----
            if method == "POST" and path == "/api/plans":
                body.setdefault("idempotency_key", self._idem_key(body))
                return service.create_plan(**body)
            if method == "GET" and path == "/api/plans":
                return {
                    "plans": service.list_plans(
                        year=int(q["year"]) if "year" in q else None,
                        status=q.get("status"),
                    )
                }
            if len(p) >= 4 and p[1] == "api" and p[2] == "plans":
                plan_id = need_id(p[3])
                if method == "GET" and len(p) == 4:
                    return service.get_plan(plan_id)
                if method == "PUT" and len(p) == 4:
                    return service.update_plan_items(plan_id, body.get("items", []))
                if method == "DELETE" and len(p) == 4:
                    service.delete_plan(plan_id)
                    return {"deleted": plan_id}
                if method == "POST" and len(p) == 5 and p[4] == "preview":
                    return service.preview_plan(plan_id)
                if method == "POST" and len(p) == 5 and p[4] == "submit":
                    return service.submit_plan(plan_id)
                if method == "POST" and len(p) == 5 and p[4] == "approve":
                    return service.approve_plan(plan_id)
                if method == "POST" and len(p) == 5 and p[4] == "reject":
                    return service.reject_plan(
                        plan_id,
                        reason=str(body.get("reason", "")),
                        idem_key=self._idem_key(body),
                    )
                if method == "POST" and len(p) == 5 and p[4] == "issue":
                    return service.issue_plan(plan_id, idem_key=self._idem_key(body))
                if method == "POST" and len(p) == 5 and p[4] == "release":
                    return service.release_resources(
                        plan_id,
                        items=body.get("items", []),
                        reason=str(body.get("reason", "")),
                        idem_key=self._idem_key(body),
                    )
                if method == "GET" and len(p) == 5 and p[4] == "allocations":
                    return {"allocations": service.list_allocations(plan_id)}
                if method == "GET" and len(p) == 5 and p[4] == "ledger":
                    return {"entries": service.list_ledger_entries(plan_id=plan_id)}

            # ---- 调剂 ----
            if method == "POST" and path == "/api/transfers":
                try:
                    from_id = int(body["from_plan_id"])
                    to_id = int(body["to_plan_id"])
                except (KeyError, TypeError, ValueError):
                    raise DomainError("from_plan_id 与 to_plan_id 必须是整数")
                return service.transfer_resources(
                    from_plan_id=from_id,
                    to_plan_id=to_id,
                    items=body.get("items", []),
                    reason=str(body.get("reason", "")),
                    idem_key=self._idem_key(body),
                )
            if method == "GET" and len(p) == 4 and p[1] == "api" and p[2] == "transfers":
                return service.get_transfer(need_id(p[3]))

            # ---- 批次 / 恢复 / 对账 ----
            if method == "GET" and len(p) == 4 and p[1] == "api" and p[2] == "batches":
                return service.get_batch(need_id(p[3]))
            if method == "POST" and path == "/api/recover":
                return service.recover()
            if method == "POST" and path == "/api/reconcile":
                return service.reconcile()
            if method == "GET" and path == "/api/ledger":
                account_id = int(q["account_id"]) if "account_id" in q else None
                return {"entries": service.list_ledger_entries(account_id=account_id)}
            if method == "GET" and path == "/healthz":
                return {"status": "ok"}

            raise NotFoundError(f"没有匹配的路由：{method} {path}")

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._handle("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle("DELETE")

    return Handler


def build_server(host: str, port: int, db_path: str) -> tuple[ThreadingHTTPServer, App]:
    app = App(db_path)
    handler = make_handler(app)
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd, app
