"""命令行入口：PYTHONPATH=src python3 -m allocation --db data/alloc.db --port 8080"""
from __future__ import annotations

import argparse

from .api import run


def main() -> None:
    parser = argparse.ArgumentParser(prog="allocation", description="高校资源分类配置服务端")
    parser.add_argument("--db", default="data/allocation.db", help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8080, help="监听端口")
    args = parser.parse_args()
    run(args.host, args.port, args.db)


if __name__ == "__main__":
    main()
