"""写入一套演示数据（财政+教育联合工作组场景）。

用法：
    PYTHONPATH=src python3 tools/seed_demo.py alloc.sqlite3

幂等：重复执行不会重建同名账户/项目（已存在则跳过）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from resource_allocation.db import connect, init_db
from resource_allocation.errors import ConflictError
from resource_allocation.service import ResourceService


def seed(svc: ResourceService) -> None:
    # 2026 年度三本账：专项资金（分）、实验条件（间）、师资名额（人）
    accounts = [
        ("fund", "中央高校专项", 100_000_000, 80_000_000, [
            ("人工智能", 4000), ("集成电路", 4000), ("基础学科", 2000),
        ]),
        ("lab", "国家重点实验室条件包", 200, 120, [
            ("人工智能", 5000), ("集成电路", 5000),
        ]),
        ("faculty", "联合师资名额", 300, 20, [
            ("人工智能", 5000), ("集成电路", 5000),
        ]),
    ]
    for kind, name, total, mpp, tracks in accounts:
        try:
            svc.create_account(
                year=2026, kind=kind, name=name, total_amount=total,
                max_per_project=mpp,
                tracks=[{"track": t, "guarantee_bps": b} for t, b in tracks],
            )
            print(f"建账：{name}（{kind}，总额 {total}）")
        except ConflictError:
            print(f"已存在，跳过：{name}")

    projects = [
        ("U-AI-01", "东海大学人工智能学院", "人工智能"),
        ("U-IC-01", "西山大学集成电路中心", "集成电路"),
        ("U-BS-01", "南岭大学数理平台", "基础学科"),
    ]
    for code, name, track in projects:
        try:
            svc.create_project(code=code, name=name, track=track)
            print(f"建项目：{code} {name}（{track}）")
        except ConflictError:
            print(f"项目已存在，跳过：{code}")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "alloc.sqlite3"
    conn = connect(path)
    init_db(conn)
    seed(ResourceService(conn))
    conn.close()
    print(f"演示数据就绪：{path}")
