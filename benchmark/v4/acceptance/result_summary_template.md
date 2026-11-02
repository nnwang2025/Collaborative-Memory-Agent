# Phase 4 真模型验收结果汇总（模板）

- plan_version: `cma-v4-acceptance-v1`
- snapshot: `cma-v4-658dd583ab18a47f`
- provider/model: `<填写>`
- run_id (React): `<填写>`
- run_id (Workflow): `<填写>`
- 执行日期: `<YYYY-MM-DD>`
- 是否发生 402/429/503: `<填写>`
- 实际模型调用: React `<n>` / Workflow `<n>`（规划上界 77 / 33）
- 实际 provider tokens: React `<n>` / Workflow `<n>`
- 实际成本: `<null 或按定价表计算>`

## 运行状态

| backend | selected tasks | completed | stopped/reason | model calls | tokens |
|---|---|---|---|---|---|
| react | 10 |  |  |  |  |
| workflow | 10 |  |  |  |  |

## 自动指标（不是 TaskSuccessRate）

| metric | react | workflow | coverage |
|---|---|---|---|
| EvidenceRecall@K |  |  |  |
| CitationValidity |  |  |  |
| FactAccuracy |  |  |  |
| AppropriateAbstentionRate |  |  |  |
| ConflictDetectionRate |  |  |  |
| DecisionStateAccuracy |  |  |  |
| ToolFailureRecoveryRate |  |  |  |

## 人工评分汇总

| metric | react | workflow | 分母 |
|---|---|---|---|
| TaskSuccessRate |  |  | 10 |
| Semantic citation support |  |  |  |
| Conflict interpretation correct |  |  |  |
| Decision cutoff correct |  |  |  |

说明：TaskSuccessRate 仅在 20 个 `(task_id, backend)` 均有具名 reviewer 的显式判断后才计算。

## 结果解读

- 只报告同一模型、同一快照、同一任务集、同一 Scope、同一 scorer、同一预算上限下的端到端差异。
- Workflow 在预算协议、Claim Ledger、known_at、调查轮次上不可比；不得把差异归因于 ReAct 推理机制的因果提升。
- 任何索引回退、离线 fallback、402 中止或未完成运行都必须单列，不进入成功样本。
- 语义支持、冲突解释和决策解释只能由人工判断，不能由自动分数替代。
