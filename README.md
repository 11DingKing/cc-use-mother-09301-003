# 高校资源分类配置服务端

财政与教育部门联合下达专项资金、实验条件与师资名额的服务端。解决共享表格的三类顽疾：
**额度被重复占用、调减没有可解释依据、中断后续办重复扣减**。

纯 Python 3.11 标准库实现（无第三方依赖），SQLite 持久化，自带 HTTP API、命令行与完整测试。

## 核心机制

| 需求 | 实现 |
| --- | --- |
| 按年度预算、适用赛道、保底比例、限制条件建账 | `accounts` + `account_tracks`（保底比例以基点 0..10000 表示）+ 每项目上限 |
| 试算方案彼此隔离 | 草案明细只落在 `plan_items`，**完全不进台账**；`POST /plans/{id}/preview` 纯读校验 |
| 正式下达原子锁定额度 | `BEGIN IMMEDIATE` 串行化 + 单事务整体提交；任一额度校验失败整体回滚 |
| 留下不可变清单 | `allocations` 与 `ledger_entries` 触发器禁止 UPDATE/DELETE；台账带 **SHA-256 哈希链**，绕过触发器改库也会在对账时暴露 |
| 申请被退回 | 下达前退回仅扭转状态、不占额度；下达后整单退回生成 `reject_release` 分录回收未结清额，退回依据 `reason` 永久留痕 |
| 只释放部分资源 | 按"不可变清单 id + 数量 + 模式"调减；`release` 回收额度可再分，`write_off` 已据实支出、核销不回收 |
| 跨项目调剂 | 单事务内 `transfer_out` + `transfer_in` 成对落账，生成接收方新清单；同年度、同账户适用赛道才允许 |
| 恢复续办不重复扣减 | 操作先持久化"意图批次"（batches/batch_items、transfers），再执行落账；崩溃后 `recover()` 续办，已提交部分不重做、已回滚事务重放安全；所有写操作带幂等键 |

### 台账模型（追加式）

每条分录带两个带符号增量：账户占用 `used_delta` 与清单未结清额 `outstanding_delta`。

| 分录 | used_delta | outstanding_delta |
| --- | --- | --- |
| `reserve`（下达锁定） | +amount | +amount |
| `transfer_in`（调剂划入） | +amount | +amount |
| `reject_release`（退回） | −amount | −amount |
| `release`（部分释放） | −amount | −amount |
| `write_off`（核销） | 0 | −amount |
| `transfer_out`（调剂划出） | −amount | −amount |

保底可行性：落账后 `剩余可用 >= 各赛道保底缺口之和`，即任何赛道不能挤占其他赛道的法定保底额度。

## 目录

- `domain/contract.json`：领域角色、状态、约束与样例（契约层）。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/resource_allocation/`
  - `models.py`：资源类型、方案/批次状态、台账分录类型。
  - `db.py`：建表、不可变触发器。
  - `ledger.py`：哈希链台账、占用查询、链校验。
  - `service.py`：领域服务（建账、试算、审批、下达、退回、释放、调剂、恢复、对账）。
  - `api.py` / `server.py`：HTTP API 与启动入口。
- `tools/check_contract.py`：契约摘要检查；`tools/seed_demo.py`：演示数据。
- `tests/`：56 个回归测试（领域逻辑、并发抢占、崩溃恢复、防篡改、HTTP 端到端）。

## 启动

```bash
# 初始化演示数据（三本账 + 三个高校项目）
PYTHONPATH=src python3 tools/seed_demo.py alloc.sqlite3

# 启动服务（启动时自动 recover 未完成批次）
PYTHONPATH=src python3 -m resource_allocation.server --host 0.0.0.0 --port 8080 --db alloc.sqlite3
```

## HTTP API

所有写操作必须带 `Idempotency-Key` 头（或请求体 `idempotency_key`）。重试同键返回首次结果，绝不产生第二笔扣减。金额/数量一律为正整数（资金单位：分）。

```text
POST   /api/accounts                      建账（year/kind/name/total_amount/tracks/max_per_project）
GET    /api/accounts?year=2026
GET    /api/accounts/{id}
GET    /api/accounts/{id}/snapshot        总额度/占用/可用/按赛道占用
POST   /api/projects                      code/name/track
GET    /api/projects

POST   /api/plans                          创建试算草案（project_code/year/title/items）
GET    /api/plans?year=&status=
GET    /api/plans/{id}
PUT    /api/plans/{id}                     修改草案明细
DELETE /api/plans/{id}                     删除草案
POST   /api/plans/{id}/preview             纯读试算：赛道适用/项目上限/额度/保底可行性
POST   /api/plans/{id}/submit              申报
POST   /api/plans/{id}/approve             审批通过
POST   /api/plans/{id}/reject              退回（body.reason 必填，可解释依据）
POST   /api/plans/{id}/issue               正式下达，原子锁定、生成不可变清单
POST   /api/plans/{id}/release             部分释放/核销
                                           items=[{allocation_id, amount, mode=release|write_off}]
GET    /api/plans/{id}/allocations         不可变清单及每条的完整事件轨迹
GET    /api/plans/{id}/ledger

POST   /api/transfers                      跨项目调剂
                                           from_plan_id/to_plan_id/items[{allocation_id,amount}]/reason
GET    /api/transfers/{id}
GET    /api/batches/{id}                   批次状态与每条明细的执行状态
POST   /api/recover                        续办未完成批次/调剂（启动时自动执行）
POST   /api/reconcile                      账实核对：哈希链 + 额度非负 + 清单余额 + 状态一致
GET    /api/ledger?account_id=             全量台账流水
GET    /healthz
```

### 典型流程

```bash
# 1. 试算（不落账）
curl -X POST .../api/plans/1/preview
# 2. 申报 → 审批 → 下达（带幂等键）
curl -X POST .../api/plans/1/submit
curl -X POST .../api/plans/1/approve
curl -X POST .../api/plans/1/issue -H 'Idempotency-Key: issue-2026-001' -d '{}'
# 3. 调减必须写明依据
curl -X POST .../api/plans/1/release -H 'Idempotency-Key: rel-2026-007' -d '{
  "items": [{"allocation_id": 1, "amount": 1000000, "mode": "release"}],
  "reason": "设备采购调减-财政2026-17号文"}'
# 4. 跨项目调剂
curl -X POST .../api/transfers -H 'Idempotency-Key: tr-2026-003' -d '{
  "from_plan_id": 1, "to_plan_id": 2,
  "items": [{"allocation_id": 1, "amount": 500000}],
  "reason": "联席会议统筹调剂-第3次"}'
# 5. 随时对账
curl -X POST .../api/reconcile
```

## 崩溃与并发语义

- **并发下达**：`BEGIN IMMEDIATE` 立即取写锁，两个竞争方案只有一个能成功占用，另一个收到 `422 quota_exceeded`，总额度绝不超用（有双连接真实线程测试）。
- **执行中崩溃**：意图批次已落盘、执行事务回滚 → 重启 `recover()` 续办；已 `done` 明细跳过、未落账明细重做，结果只扣一次。
- **停机期间额度被他人合法占用**：续办重验失败，批次转 `failed` 并记录原因，方案保持可修改状态，账实仍平衡。
- **业务失败 vs 基础设施失败**：规则违反（额度不足/状态不对）标记批次永久 `failed`；进程崩溃/IO 异常保留 `running` 等待续办。

## 验证

```bash
python3 -m unittest discover -s tests -v     # 56 个测试
python3 -m compileall -q src tools tests     # 编译检查
python3 tools/check_contract.py domain/contract.json
```
