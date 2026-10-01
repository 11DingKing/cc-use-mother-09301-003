# 高校资源分类配置

本项目维护高校资源分类配置的领域约定、角色边界与样例数据，并提供一套完整的服务端，供财政专员、教育部门、高校经办人组成的联合工作组统一管理专项资金、实验条件与师资名额。核心约束：年度额度、原子占用、方案比较、恢复续办。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/allocation/`：资源分类配置服务端（建账、试算、下达、退回、调剂、对账、恢复）。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/run_server.py`：启动服务端（启动时自动恢复未完成批次）。
- `tools/demo_flow.py`：端到端业务演示（含模拟崩溃恢复）。
- `tests/`：契约与服务端回归测试。

## 服务端

零第三方依赖（Python ≥ 3.11 标准库 + SQLite）。启动：

```bash
python3 tools/run_server.py --db data/allocation.db --port 8080
# 或：PYTHONPATH=src python3 -m allocation --db data/allocation.db --port 8080
```

### 业务流与接口

| 环节 | 接口 | 说明 |
| --- | --- | --- |
| 建账 | `POST /pools` | 按年度预算、适用赛道、保底比例（`floor_ratio` 或 `floor_ratio_bp`）、限制条件建资源池 |
| 申报 | `POST /applications` | 高校提交申报（高校、赛道、需求） |
| 试算 | `POST /plans`、`POST /plans/{id}/items`、`GET /plans/{id}/simulate` | 方案只读投影真账，彼此隔离，不占额度 |
| 方案比较 | `POST /plans/compare` | 多方案在同一资源池上的占用投影对照 |
| 审批 | `POST /batches/{id}/approve` | 批次草稿 → 已审批 |
| 下达 | `POST /batches`、`POST /batches/{id}/issue` | 逐行原子锁定额度；`GET /batches/{id}/manifest` 取不可变清单（含 SHA-256 摘要） |
| 调减 | `POST /batches/{id}/lines/{line}/adjust` | 未下达行调减，留痕当时可用/保底/锁定快照与理由 |
| 退回/部分释放 | `POST /applications/{id}/return`、`POST /holdings/{id}/release` | 全量或部分释放，账实同步 |
| 跨项目调剂 | `POST /reallocations` | 同池或跨池（同年度同类型）转移占用，受保底比例与限制条件约束 |
| 对账 | `GET /reconcile` | 池锁定 = 在账占用之和 = 台账有符号流水之和 |
| 恢复续办 | `POST /admin/recover` | 继续处理"下达中"批次；服务启动时自动执行 |

辅助查询：`GET /pools`、`GET /pools/{id}`（含可用/保底/可调剂上限）、`GET /pools/{id}/holdings`、`GET /pools/{id}/ledger`、`GET /applications/{id}`、`GET /plans/{id}`、`GET /batches`、`GET /batches/{id}`、`GET /health`。

### 关键设计

- **不重复占用**：单写者事务（`BEGIN IMMEDIATE`）+ 下达时可用额度校验 + `(资源池, 申报)` 在账占用部分唯一索引，替代共享表格后从数据库层面杜绝重复占额度。
- **原子锁定与不可变清单**：每条下达行在独立事务内完成"校验 → 占用 → 台账 → 行状态"；台账由触发器禁止 UPDATE/DELETE，清单接口输出内容摘要指纹，已入账行不可调减，只能经退回/调剂冲正。
- **可解释的调减**：调减、预算修订、保底拦截都在报错或留痕中携带当时的可用额度、保底额度、锁定余额与理由。
- **恢复续办不重复扣减**：行级幂等键（`issue_key`）+ 台账幂等键唯一约束 + 批次状态机；崩溃后 `recover` 只补未入账行。变更接口另支持 `request_id` / `Idempotency-Key` 请求头做接口级幂等重试。
- **限制条件**：`restrictions` 中 `max_single_amount`（单项上限）、`per_applicant_cap`（高校在账上限）、`allowed_applicants`（允许名单）由系统强制执行，其余键原样留存。
- **金额口径**：所有金额/数量为整数，单位为资源池的 `unit`（元/项/人）。

### 示例

```bash
curl -s -X POST localhost:8080/pools -d '{"year":2026,"resource_type":"fund","tracks":["理工"],"unit":"元","budget_amount":1000000,"floor_ratio":0.2,"restrictions":{"max_single_amount":700000},"actor":"财政专员"}'
curl -s -X POST localhost:8080/applications -d '{"applicant":"甲大学","track":"理工","title":"重点实验室","demands":[{"resource_type":"fund","amount":600000}],"actor":"高校经办人"}'
curl -s -X POST localhost:8080/batches -d '{"actor":"财政专员","year":2026,"lines":[{"application_id":"APP-0001","pool_id":"POOL-0001","amount":600000}]}'
curl -s -X POST localhost:8080/batches/BATCH-0001/approve -d '{"actor":"教育部门"}'
curl -s -X POST localhost:8080/batches/BATCH-0001/issue -d '{"actor":"教育部门","request_id":"issue-0001"}'
curl -s localhost:8080/batches/BATCH-0001/manifest
curl -s localhost:8080/reconcile
```

端到端演示（无需启动服务）：`python3 tools/demo_flow.py`

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
