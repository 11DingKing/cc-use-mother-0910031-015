# 讲解员成长等级

本项目维护讲解员成长等级的领域约定、角色边界与样例数据，并提供一套可直接运行的
**append-only 成长等级后端**：以复式分录记录积分、以流水记录升降级、以快照核对余额，
确保评价撤回、积分冲回、等级保护期与人工复核都不会改写历史，已发权益始终可核对。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/growth_ledger/`：成长等级后端（零外部依赖，仅需 Python 3.11+ / SQLite）。
  - `models.py`：不可变领域模型（规则版本、事件、分录、等级流水、权益、复核、快照）。
  - `engine.py`：纯计算——复式平衡、时间线重放、保护期、快照哈希、解释文案。
  - `store.py`：SQLite append-only 存储，状态字段仅允许带条件的受控更新（CAS）。
  - `service.py`：应用服务，全部写操作在单事务内完成并强制守恒校验。
  - `api.py` / `__main__.py`：标准库 HTTP 接口与启动入口。
- `tools/check_contract.py`：契约命令行检查。
- `tools/run_demo.py`：端到端业务场景演示。
- `tests/`：契约、领域服务、HTTP 接口回归测试。

## 设计要点（对应四条领域不变量）

1. **成长规则版本**：规则含等级区间、保护期天数与权益清单；版本一经发布不可修改，
   调整阈值只能发布新版本，每笔等级流水都记录所依据的 `rule_version`。
2. **积分事件分录**：每个业务事件写一对**借贷平衡**分录：
   - 正向事件：`EARNED -n`（收入贷方）/ `AVAILABLE +n`（可用借方）；
   - 冲回事件（评价撤回/更正）：`AVAILABLE -n` / `REVERSED +n`（抵减科目），
     原事件标记 `REVERSED` 但**原样保留**，毛获得额不变。
   - 全局恒等式：`AVAILABLE + EARNED + REVERSED == 0`。
3. **等级保护期**：积分事件不直接改等级，等级只来自升降级流水。冲回后候选等级
   回落时，若仍在保护期内，只写一条 `PROTECTION_HELD` 决策分录维持等级与权益；
   保护期结束后的定时任务才降级。
4. **权益变动解释**：每次权益 `GRANT/REVOKE` 都指向来源等级分录，等级流水的
   `explanation` 给出积分构成、被冲回单据、规则版本、区间与触发方。

另外：

- **快照**：每次评估写余额快照，含内容哈希与前序哈希（哈希链）。内容未变化不重复
  落快照；任何对历史余额的篡改都会被 `/audit/conservation` 巡检发现。
- **定时晋级可安全重跑**：以 `(run_date, rule_version)` 为幂等键，重跑直接返回
  首次结果，不产生重复分录；同一天内复核引发的评估也不会被定时任务重复记账。
- **人工复核**：`RATING_CORRECTION`（可更正为新分值：先红冲再重记）、
  `POINT_REVERSAL`、`LEVEL_OVERRIDE`、`PROTECTION_DISPUTE`，驳回不改任何余额。
- **事务守卫**：任一写操作提交前若不满足守恒或快照链异常，整体回滚。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/rules` | 发布规则版本（bands/effective_from） |
| GET  | `/rules` | 规则版本列表 |
| POST | `/guides` | 讲解员建档（自动建初始等级分录与快照） |
| GET  | `/guides` | 讲解员列表 |
| POST | `/guides/{id}/events` | 登记积分事件（source_ref 唯一，支持 idempotency_key） |
| GET  | `/guides/{id}/events` | 积分事件（含 REVERSED 状态） |
| GET  | `/guides/{id}/entries` | 复式分录明细 |
| GET  | `/guides/{id}/evaluate?as_of=YYYY-MM-DD` | 只读重放：当前/候选等级、余额、权益、积分构成 |
| GET  | `/guides/{id}/timeline` | 完整档案：事件、分录、升降级、权益、快照、守恒报告 |
| GET  | `/guides/{id}/snapshots` | 余额快照哈希链 |
| POST | `/guides/{id}/reviews` | 发起人工复核（kind/event_id/payload） |
| POST | `/reviews/{id}/decision` | 复核结论（approved + decided_by） |
| GET  | `/reviews` | 复核单列表 |
| POST | `/promotions/run` | 定时晋级（body/query 传 run_date），幂等可重跑 |
| GET  | `/audit/conservation` | 全局余额守恒与快照哈希链巡检 |

## 运行

```bash
# 启动 HTTP 服务（默认 127.0.0.1:8080，SQLite 文件 growth.db）
PYTHONPATH=src python3 -m growth_ledger --port 8080 --db growth.db

# 场景演示：晋级 → 评价撤回（保护期不降）→ 保护期后降级与权益撤销
python3 tools/run_demo.py
```

调用示例：

```bash
curl -X POST localhost:8080/rules -H 'Content-Type: application/json' -d '{
  "effective_from": "2026-01-01", "published_by": "admin",
  "bands": [
    {"level": 1, "name": "见习", "min_points": 0,  "protection_days": 0,  "benefits": []},
    {"level": 2, "name": "初级", "min_points": 30, "protection_days": 30, "benefits": ["培训优先报名"]}
  ]}'
```

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
