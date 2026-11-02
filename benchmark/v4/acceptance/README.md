# Phase 4 小规模正式真模型验收准备

- plan_version: `cma-v4-acceptance-v1`
- status: `prepared_not_executed`
- snapshot: `cma-v4-658dd583ab18a47f`
- task_set: `cma-v4-tasks-v1`
- tasks: 10
- 本轮未调用任何付费模型，未读取 API Key。

## 任务选择

| task_id | category | difficulty | turns | reason |
|---|---|---|---|---|
| v4-single-source-01 | single_source | easy | 1 | Clean single-source baseline: required facts must come from one source family without cross-source fusion. |
| v4-cross-source-01 | cross_source | medium | 1 | Cross-source task: the answer needs at least two source types, exercising multi-skill retrieval, merge and cross-source citation. |
| v4-multi-step-01 | multi_step | medium | 1 | Multi-step investigation: several constraints/entities must be resolved before the final fact can be supported. |
| v4-decision-history-01 | decision_history | medium | 1 | Historical decision cutoff: distinguishes a confirmed version at known_at from a later proposal that must not be reported as confirmed. |
| v4-conflict-01 | conflict | medium | 1 | Unresolved conflict: both sides must be disclosed without the system choosing one side as the verdict. |
| v4-insufficient-01 | insufficient | hard | 1 | Insufficient evidence: the correct behaviour is a bounded abstention that discloses what is missing, not an invented answer. |
| v4-multi-turn-01 | multi_turn | medium | 2 | Two-turn follow-up: tests per-turn evaluation targets and whether conversation context is used without treating the previous answer as evidence. |
| v4-tool-failure-01 | tool_failure | hard | 1 | Injected document-tool timeout: tests recovery or explicit disclosure after a source tool fails. |
| v4-tool-failure-02 | tool_failure | hard | 1 | Injected problem-tool timeout: a second, different failing skill, so the result does not depend on one failure point. |
| v4-budget-01 | budget | hard | 1 | Explicit budget override: tests whether the backend stops within the configured limit and discloses the unfinished part instead of claiming completion. |

## 覆盖

- categories: budget, conflict, cross_source, decision_history, insufficient, multi_step, multi_turn, single_source, tool_failure
- single-turn: 9
- multi-turn: 1
- tool-failure tasks: 2
- budget tasks: 1

## 实验配置

- backends: react, workflow
- retrieval_mode: `hybrid`
- index: 每个项目构建并校验真实 FAISS；失败或回退即中止。
- provider/model: operator_must_supply
- temperature: 0.1
- max_output_tokens: 1024
- max_retries: 1

| backend | max tasks | max model calls | max budget tokens | rounds |
|---|---|---|---|---|
| react | 10 | 80 | 300000 | 5 |
| workflow | 10 | 80 | 300000 | None |

调用次数上限（规划上界）：ReAct 77，Workflow 33；总 turn 数 11。

## Token 与成本估算方法

- 事前：calls_upper_bound * (estimated_input_tokens + max_output_tokens); estimated_input_tokens = UTF-8 bytes of the request JSON // 4. Treat as a planning ceiling, not a measurement.
- 事后：Use provider usage.prompt_tokens/completion_tokens when present; otherwise estimate bytes // 4 and mark estimated_usage=true.
- 成本：actual_cost_usd = prompt_tokens * input_unit_price + completion_tokens * output_unit_price. Unit prices must be supplied by the operator for the exact model; no price is assumed here.
- 本轮无定价表，实际成本保持 `null`。

## 可比性边界

- 不可比：budget-protocol behaviour
- 不可比：Claim Ledger validation
- 不可比：known_at filtering
- 不可比：multi-round investigation depth

## 产物

- `plan.json`：机器可读选择与配置。
- `gold_review_checklist.md` / `.json`：逐任务人工复核清单。
- `grading_template.csv`：`(task_id, backend)` 人工评分表，初始全部为空白。
- `result_summary_template.md`：结果汇总模板。
- `termination_recovery.md`：终止与恢复流程。
- 评分表未经人工填写前，`TaskSuccessRate` 必须保持 `null`。
