"""端到端演示：建账 → 申报 → 试算 → 审批下达 → 部分释放 → 调剂 → 对账 → 崩溃恢复。

直接运行：python3 tools/demo_flow.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from allocation import AllocationService


def show(title: str, payload) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> None:
    db_path = str(Path(tempfile.mkdtemp()) / "demo.db")
    svc = AllocationService(db_path)

    # 1. 建账：专项资金池（保底 20%）与师资名额池
    fund = svc.create_pool(
        year=2026, resource_type="fund", tracks=["理工"], unit="元",
        budget_amount=1_000_000, floor_ratio=0.2,
        restrictions={"max_single_amount": 700_000}, actor="财政专员",
    )
    show("1. 资源池建账（年度预算/赛道/保底/限制条件）", fund)

    # 2. 高校申报
    app_a = svc.create_application(
        applicant="甲大学", track="理工", title="重点实验室建设",
        demands=[{"resource_type": "fund", "amount": 600_000}], actor="高校经办人",
    )
    app_b = svc.create_application(
        applicant="乙大学", track="理工", title="工程中心",
        demands=[{"resource_type": "fund", "amount": 300_000}], actor="高校经办人",
    )

    # 3. 两套试算方案彼此隔离，可并排比较
    plan1 = svc.create_plan(name="方案一：甲 60 万", year=2026, actor="教育部门")
    svc.add_plan_item(plan_id=plan1["plan_id"], application_id=app_a["application_id"],
                      pool_id=fund["pool_id"], amount=600_000)
    plan2 = svc.create_plan(name="方案二：甲乙各 30 万", year=2026, actor="教育部门")
    svc.add_plan_item(plan_id=plan2["plan_id"], application_id=app_a["application_id"],
                      pool_id=fund["pool_id"], amount=300_000)
    svc.add_plan_item(plan_id=plan2["plan_id"], application_id=app_b["application_id"],
                      pool_id=fund["pool_id"], amount=300_000)
    show("3. 方案比较（试算不占真账）", svc.compare_plans([plan1["plan_id"], plan2["plan_id"]]))
    print(f"   试算后真账锁定仍为：{svc.get_pool(fund['pool_id'])['locked_amount']}")

    # 4. 方案二转批次，审批后正式下达（原子锁定 + 不可变清单）
    batch = svc.create_batch(actor="财政专员", plan_id=plan2["plan_id"])
    svc.approve_batch(batch_id=batch["batch_id"], actor="教育部门")
    issued = svc.issue_batch(batch_id=batch["batch_id"], actor="教育部门")
    show("4. 正式下达结果", issued)
    show("   不可变清单（含防篡改摘要）", svc.batch_manifest(batch["batch_id"]))

    # 5. 部分释放 + 跨项目调剂，账实始终一致
    holding_a = svc.get_application(app_a["application_id"])["holdings"][0]
    svc.release_holding(holding_id=holding_a["holding_id"], amount=50_000,
                        reason="甲大学阶段调减", actor="财政专员")
    svc.reallocate(from_holding_id=holding_a["holding_id"],
                   to_application_id=app_b["application_id"], amount=100_000,
                   reason="乙大学设备提前到位，同池调剂", actor="教育部门")
    show("5. 部分释放 + 调剂后的对账", svc.reconcile())

    # 6. 模拟崩溃恢复：批次下达两行后进程退出，重启续办不重扣
    pool2 = svc.create_pool(
        year=2026, resource_type="faculty", tracks=["理工"], unit="人",
        budget_amount=30, floor_ratio_bp=0, restrictions={}, actor="财政专员",
    )
    apps = [svc.create_application(applicant=f"高校{i}", track="理工", title="引才计划",
                                   demands=[], actor="高校经办人") for i in "CDE"]
    batch2 = svc.create_batch(
        actor="财政专员", year=2026,
        lines=[{"application_id": a["application_id"], "pool_id": pool2["pool_id"], "amount": 5}
               for a in apps],
    )
    svc.approve_batch(batch_id=batch2["batch_id"], actor="教育部门")
    original = svc._apply_line
    calls = {"n": 0}

    def flaky(conn, batch_id, line_id, actor):
        if calls["n"] >= 2:
            raise RuntimeError("模拟进程崩溃")
        calls["n"] += 1
        return original(conn, batch_id, line_id, actor)

    svc._apply_line = flaky
    try:
        svc.issue_batch(batch_id=batch2["batch_id"], actor="教育部门")
    except RuntimeError as exc:
        print(f"\n=== 6. 模拟崩溃：{exc} ===")
    print(f"   崩溃时批次状态：{svc.get_batch(batch2['batch_id'])['status']}，"
          f"已锁定 {svc.get_pool(pool2['pool_id'])['locked_amount']} 人")
    restarted = AllocationService(db_path)  # 服务重启
    show("   重启后恢复续办", restarted.recover())
    print(f"   续办后锁定 {restarted.get_pool(pool2['pool_id'])['locked_amount']} 人"
          f"（应为 15，无重复扣减）")
    show("   全库对账", restarted.reconcile())


if __name__ == "__main__":
    main()
