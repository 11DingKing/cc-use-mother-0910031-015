# 讲解员成长等级后端

依据**服务时长、评价、主题能力**调整讲解员等级的完整后端。核心诉求是：评价撤回、
积分冲回、等级保护期、人工复核都**不能改写历史、不能破坏余额守恒**；定时晋级
**可安全重跑**；每一次升降级与权益变化都能解释来源。

- 纯 Python 3.11 标准库实现（SQLite + `http.server`），无第三方依赖。
- 覆盖领域契约四不变量：**成长规则版本、积分事件分录、等级保护期、权益变动解释**。

## 领域模型与不变量

### 1. 成长规则版本（不可变、仅追加）

`rule_versions` 只追加：参数（保护期天数、预警次数）、积分策略（时长换算、星级
分值）、等级区间（积分/场次/均分/主题能力门槛）、权益定义，整体做内容哈希。
积分事件、评定单、权益授予都记录所依据的 `rule_version`；发布新版本**不重算**
任何历史分录与快照。

### 2. 积分事件与复式分录（守恒）

每个事件写一组**借贷配平**的分录：

| 账户 | 借记 | 贷记 |
|---|---|---|
| `POINTS:G{id}`（讲解员） | 积分减少 | 积分增加 |
| `FUND:POINTS_ISSUED`（发行账户） | 积分发行 | 积分冲回 |

- 全体账户余额之和恒为 0：`GET /integrity` 逐事件校验借贷相等、讲解员总积分与
  发行账户轧差为 0。
- **撤回 / 取消**：追加一组方向相反的冲回分录（`reversal` 事件），旧事件保留并
  置 `reversed`；**更正**：仅追加“新旧积分差额”分录，旧评价置 `corrected`。
  因此用 `occurred_at <= as_of` 可重放任意历史时点的真实余额。
- 讲解员余额允许为负：追扣已消费积分形成“积分欠账”，在对账单与
  `negative_balances` 中显式暴露，由人工调整（`manual_adjust`，带幂等键）结清。

### 3. 等级区间与保护期

评定只读指标，写一条不可变 `level_evaluations`；等级变化另开一条
`level_assignments` 区间（旧区间收口，不删除）。

- 晋级：满足目标等级全部门槛（积分、场次、均分、主题能力）。
- 晋级后 `protection_days_after_promotion` 天内**免于降级**。
- 不达标先给 `demote_grace_warnings` 次**预警**，再**逐级**降级（不跨级）。
- 降级后 `protection_days_after_demotion` 天冷静期，不连续降级。
- 存在待处理**人工复核**时自动评定挂起（`review`）；复核可确认降级（跳过保护期
  与预警）或维持等级；无论哪种结论都**不动积分分录**。

### 4. 权益授予与来源解释

- 晋级补发新等级（含跨越等级）的全部权益；降级按权益自身的
  `revoke_on_demotion` 决定收回或保留（如高阶资格可保留）。
- 每条授予/收回都挂在评定单上（`granted_evaluation_id` /
  `revoked_evaluation_id`），评定单的 `effects` 给出逐条原因，形成双向来源链。
- 每次评定写一条账户**快照**（余额、等级、权益、规则版本），快照以
  `prev_digest` 串成 SHA-256 哈希链，`/snapshots/verify` 重算校验，篡改即现形。

### 5. 定时晋级可安全重跑

两层幂等：`job_runs.run_id` 唯一 + `level_evaluations(run_id, guide_id)` 唯一。
同一 `run_id` 重跑：已完成则原样回放、零写入；崩溃停留在 `running` 则从断点
续跑，不产生重复分录/等级行/权益。

## 目录

- `domain/contract.json`：领域角色、状态、约束与样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/growth_ladder/`：成长等级后端
  - `rules.py` 规则版本；`ledger.py` 积分复式分录；`engine.py` 等级评定/保护期/
    权益对账/快照；`jobs.py` 定时晋级；`store.py` SQLite 存储与守恒自检；
    `service.py` 应用门面；`api.py` HTTP 接口。
- `tools/check_contract.py`：契约摘要检查。
- `tools/seed_demo.py`：端到端演示数据（晋级→差评→保护期→撤回/更正→预警→
  人工复核降级→重放→快照链→守恒自检）。
- `tests/`：契约与后端不变量回归测试。

## 运行

```bash
# 端到端演示（建库并打印各批次、冲回分录、复核结论、快照链、守恒自检）
python3 tools/seed_demo.py data/growth.db

# 启动 HTTP 服务（默认 127.0.0.1:8080，可用 GROWTH_HOST/GROWTH_PORT/GROWTH_DB）
PYTHONPATH=src python3 -m growth_ladder.api --db data/growth.db --port 8080
```

## 主要接口

| 方法 & 路径 | 说明 |
|---|---|
| `POST /guides` | 建档讲解员 |
| `POST /guides/{id}/services` | 登记服务场次（时长、主题）→ 积分分录 |
| `POST /guides/{id}/reviews` | 登记评价（1–5 星，可能负分） |
| `POST /reviews/{uid}/withdraw` | 评价撤回 → 冲回分录 |
| `POST /reviews/{uid}/correct` | 评价更正 → 差额分录 |
| `POST /sessions/{uid}/cancel` | 场次取消 → 冲回服务积分 |
| `POST /guides/{id}/adjustments` | 人工调整（必带理由、幂等键） |
| `GET  /guides/{id}/balance?as_of=` | 时点余额（历史可重放） |
| `GET  /guides/{id}/metrics?as_of=` | 时点成长指标 |
| `GET  /guides/{id}/statement?as_of=` | 逐笔分录对账单（含累计余额） |
| `POST /guides/{id}/evaluations` | 单次评定（手动） |
| `POST /cycles/run` | 定时晋级批次（可传 `run_id` 重跑） |
| `GET  /runs` | 批次记录 |
| `POST /guides/{id}/holds` / `POST /holds/{id}/resolve` | 发起 / 处理人工复核 |
| `GET  /evaluations/{id}` | 评定单：指标、逐条理由、权益效果 |
| `GET  /guides/{id}/assignments` `/benefits` `/snapshots` | 等级区间 / 权益 / 快照 |
| `GET  /guides/{id}/snapshots/verify` | 快照哈希链校验 |
| `GET  /guides/{id}/explain` | 当前状态的完整来源解释 |
| `GET  /integrity` | 全局借贷守恒自检 |
| `GET/POST /rules` | 列规则版本 / 发布新版本 |

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json
```
