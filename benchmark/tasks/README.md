# 任务级 Agent 质量评测（第三批第一阶段）

本目录是在既有冻结 Benchmark（`benchmark/`，14 道测试题）之上增加的**任务级评测层**。它不替换、不修改原 Benchmark、原报告或原标签，用独立题目验证“端到端任务是否完成”，而不是只看检索命中或引用数量。

- 题目集版本：`cma-task-eval-v1`
- 语料快照：`cma-v1-fb8e82059066d95c`（与冻结 Benchmark 相同）
- 题目数：16 道，8 类各 2 道
- 运行器边界：Runtime 只收到公开问题与项目范围，**不读取 Gold 答案、可接受 Evidence ID 或评分规则**；标签在运行结束后才由评测器连接。

## 1. 文件

| 文件 | 作用 |
| --- | --- |
| `public.json` | 16 道公开任务（问题、project、dataset_kind、类别），运行时可读 |
| `labels.json` | 隐藏标签：关键事实、可接受证据、时间/权威/冲突/弃答要求、人工复核规则 |
| `scoring.py` | 结构校验、隔离评分、聚合、失败假设诊断、运行配置记录 |
| `run_offline.py` | 离线任务评测：真实 Go 后端（workflow/react）的离线适配器，不调用模型、不联网 |
| `run_online.py` | 显式授权的真实模型 ReAct 在线评测；默认只输出计划 |
| `grade.py` | 把显式人工评分应用到已有报告，不重跑任务 |
| `tests/test_task_eval.py` | 隔离、指标、授权边界与评分流程测试 |

## 2. 任务清单

| 任务 | 类别 | 问题焦点 |
| --- | --- | --- |
| `single-atlas` | single_source | Atlas E42 TLS 故障根因，要求引用日志 |
| `single-zephyr` | single_source | Zephyr 重试策略为何只允许两次，要求技术文档 |
| `cross-atlas` | cross_source | 固定分区归属的讨论理由与代码提交互证 |
| `cross-zephyr` | cross_source | 预热覆盖问题的定位、修复，关联 Issue 与提交 |
| `multi-atlas` | multi_round | 回滚入口 → 恢复门槛 → 后续修复提交 |
| `multi-zephyr` | multi_round | 泄漏入口 → 关闭顺序 → 实际修复提交 |
| `decision-atlas` | decision_history | 截至 2025-02-20 已确认方案；当时 C 是否替代 B |
| `decision-zephyr` | decision_history | 确认版本 B→C 的演变与替代关系 |
| `authority-atlas` | reviewed_vs_discussion | 聊天讨论、评审记录、系统已审核确认的区分 |
| `authority-zephyr` | reviewed_vs_discussion | 从讨论到系统确认的完整权威链 |
| `conflict-atlas` | conflict | prod 消息确认模式的两份同日记录是否冲突 |
| `conflict-zephyr` | conflict | prod 缓存 TTL 的设计说明与值班记录冲突 |
| `missing-atlas` | insufficient | 最新确认版本能否证明生产已执行（应说明缺口） |
| `missing-zephyr` | insufficient | 七天保留窗口是否已正式批准（应说明无确认版本） |
| `recovery-atlas` | tool_failure | Issue 工具超时后换来源并说明缺口 |
| `recovery-zephyr` | tool_failure | 文档工具超时后换来源并说明缺口 |

## 3. 标签与评分口径

每个标签包含：

- `key_facts[].marker_groups`：事实覆盖的确定性标记组；每组至少命中一个同义标记才算该事实命中。**这只是字面覆盖，不是语义正确。**
- `acceptable_evidence`：可接受证据的 `evidence_id` + `content_hash`，用于召回、引用覆盖与语料归属校验。
- `required_sources` / `expected_authority`：必须观察到的来源类型与权威层级（讨论、会议记录、已审核决策、执行、原始证据）。
- `expected_temporal`：指定时点 `known_at`、当前版本、历史版本、替代关系，以及 `requires_history`（只有明确要求版本演变的题目才检查 history 视图）。
- `expected_conflict`：冲突双方的值；要求**同时披露双方**，不要求系统自行选边。
- `abstention`：是否必须弃答/说明不足、可接受的终止原因、必须披露的缺口标记。
- `required_capabilities`：`time_filters` / `history_view` / `feedback_loop` / `failure_recovery`。
- `injected_failure`：注入的工具与错误类型（仅 tool_failure 类）。
- `human_review_rules`：人工复核时必须回答的问题。

评分分两层：

1. **确定性运行指标**：协议合法率、模型调用成功率、工具调用成功率、Evidence 召回、引用有效率、引用可接受证据覆盖率、来源召回、关键事实字面覆盖、决策/时间/权威/冲突/弃答/恢复检查、延迟、调用数、Token、成本状态。
2. **人工语义结论**：`TaskSuccessRate` 与 `AppropriateAbstentionRate` **只能**来自复核人显式填写的 `task_success` / `appropriate_abstention`。没有人工评分时值为 `null`、分母为 0。

> 引用有效率为 1.0、Evidence 数量充足、事实标记全覆盖，都**不能**据此宣布任务成功。

## 4. 隔离与安全边界

- `run_offline._case()` 只传递 `case_id/category/question/project_id/dataset_kind/corpus_snapshot_id/split`；标签字段不出现在运行时输入中。
- `score_task()` 在运行结束后连接标签；报告同时记录 `scoring_isolation=labels_joined_after_run_only`。
- 离线报告明确标记 `execution_mode=offline_no_model`、`online_executed=false`、`model_calls=0`；**不得称为真实模型效果**。
- 在线入口默认不创建输出目录、不读凭据、不联网；必须同时给出 `--online`、精确匹配配置的 HTTPS `--approved-origin` 和新 `--output`。
- 在线请求禁用重定向；凭据只由 `ReActAgent` 内部读取，运行器不打印、不写报告。
- `run_online.py` **不自动加载 `.env`**；必须先在当前 PowerShell/进程环境设置 `DEEPSEEK_API_KEY`、`SILICONFLOW_API_KEY` 或 `OPENAI_API_KEY`。占位值（如 `your_api_key_here`）视为未配置，运行以 `not_executed_no_credentials` 结束。DeepSeek origin 优先 `DEEPSEEK_API_KEY`，其他 origin 只用 `SILICONFLOW_API_KEY` / `OPENAI_API_KEY`，避免跨供应商发送凭据。
- 模型调用失败时，Trace 只记录安全的 `phase`、HTTP 状态码、异常类名、供应商错误码和脱敏短消息；不记录 Authorization、请求体或完整响应正文。
- 若首个任务回退到 `offline_fallback`，在线运行立即以 `not_executed_no_effective_credentials` 结束，不产生“真实模型结果”。
- 输出目录必须不存在，避免覆盖历史报告。

## 5. 命令

离线和单元测试（默认，无模型、无网络）：

```bash
python -m unittest discover -s tests
python -m benchmark.runner --output benchmark/reports/third-batch-offline-20261101 --split test
python -m benchmark.tasks.run_offline --output benchmark/reports/third-batch-task-offline-20261101
```

在线评测默认只打印计划（安全）：

```bash
python -m benchmark.tasks.run_online
```

真实模型在线评测（**需操作者显式授权后执行**）：

```bash
python -m benchmark.tasks.run_online \
  --online \
  --approved-origin https://api.deepseek.com \
  --output benchmark/reports/third-batch-online-20261101
```

- 当前已有在线记录的 4 个任务为 `single-atlas`、`cross-atlas`、`conflict-atlas`、`decision-atlas`；其余 12 个任务只有离线结果。**没有 16 任务整体的人工成功率。**
- `decision-atlas` 第二轮在线结果的严格复评事实覆盖为 **1/2**：一个事实标记只出现在“未确认部分”标题，严格规则不把标题当作事实正文，因此未通过。
- `--approved-origin` 必须与 `DEEPSEEK_BASE_URL` / `SILICONFLOW_BASE_URL` / `OPENAI_BASE_URL` 配置的 origin 完全一致（含 scheme 与端口，不含路径）。只设置 `DEEPSEEK_API_KEY` 时解析为 `https://api.deepseek.com`。
- 默认 16 道任务、5 轮常规模型决策、最多 8 次工具调用：常规模型调用上限 80 次；另为每题保留最多 1 次“确认输出截断后”的 4096 Token 恢复调用，因此**模型调用硬上限为 96 次、工具调用上限为 128 次**。恢复调用不使用无限制重试，且不绕过协议、Claim 或引用校验。
- 缩小成本可用 `--task <id>`（可重复）或 `--max-tasks N`；`--max-model-rounds` / `--max-tool-calls` 可进一步收紧。
- 建议先跑 1–2 道：`--max-tasks 1` 或 `--task single-atlas`，确认协议与凭据后再跑全量。

人工评分（不重跑）：

```bash
python -m benchmark.tasks.grade \
  --report benchmark/reports/third-batch-online-20261101 \
  --grades path/to/grades.json \
  --output benchmark/reports/third-batch-online-20261101-graded
```

`grades.json`：

```json
{"grades": [
  {"task_id": "single-atlas", "backend": "react", "reviewer": "reviewer-1",
   "task_success": true, "notes": "根因与引用均已人工核对"}
]}
```

弃答题可另填 `appropriate_abstention`；工具失败题可另填 `tool_failure_recovery`。两者都必须是布尔值，且只对相应标签生效。

## 6. 报告内容

`run_offline` / `run_online` 生成：

- `results.jsonl`：逐任务行；包含答案、事件、Trace、指标、诊断假设，人工评分默认为 `null`。
- `summary.json`：配置（模型、provider origin、运行时间、源码 hash、Prompt hash、数据快照、题目集版本/hash、参数）、聚合指标、诊断计数、警告。
- `pending_grading.json`：仍需人工复核的任务清单。
- 在线模式另有 `traces/<task_id>.json`：逐任务完整 ReAct Trace。

## 7. 已知限制

- 离线适配器不支持 `time_filters`、`history_view`、`feedback_loop`、`failure_recovery` 的语义路由，相关任务标记为 `unsupported_offline`，但仍保留运行信号用于诊断。
- 关键事实覆盖是字面标记匹配，同义表达可能漏计；最终以人工语义评分为准。
- 成本状态固定为 `unavailable_no_pricing`，不假设任何供应商价格。
- 旧报告与旧标签不因本轮工作被改写；每次复现使用新目录。
