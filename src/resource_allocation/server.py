"""服务启动入口。

用法：
    python -m resource_allocation.server --host 0.0.0.0 --port 8080 \
        --db /var/lib/resource_allocation/alloc.sqlite3

启动时自动执行一次 recover()，续办上次中断的批次。
"""
from __future__ import annotations

import argparse
import logging

from .api import build_server


def main() -> None:
    parser = argparse.ArgumentParser(description="高校资源分类配置服务端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--db",
        default="alloc.sqlite3",
        help="SQLite 数据库路径（默认 alloc.sqlite3）",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("resource_allocation.server")

    httpd, app = build_server(args.host, args.port, args.db)
    resumed = app.service.recover()
    if resumed["resumed"]:
        log.warning("启动恢复：续办 %d 个未完成批次/调剂", len(resumed["resumed"]))
    log.info("服务监听 http://%s:%s （数据库 %s）", args.host, args.port, args.db)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("正在关闭…")
    finally:
        httpd.server_close()
        app.shutdown()


if __name__ == "__main__":
    main()
