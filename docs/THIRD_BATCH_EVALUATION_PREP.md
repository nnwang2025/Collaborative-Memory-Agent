# 第三批第一阶段：真实 Agent 质量评测准备与失败诊断

日期：2026-11-01（Windows / Python 本地环境）
状态：评测准备与离线诊断完成；**真实模型未运行**，`online_status=pending_authorization`，等待授权。

本轮没有修改 ReAct Runtime、检索算法、Evidence Store、Ledger、DecisionStore 或技能实现；新增的是独立评测层、评分流程与在线入口。真实模型效果仍为未知，离线 fallback 结果不代表模型能力。

## 0. 现有评测能力核实结论

| 问题 | 结论 | 证据 |
| --- | --- | --- |
| 为什么 `TaskSuccessRate` 仍为 null | 旧 Benchmark 只保存 `human_task_success` 字段，从未有复核人填写；分母为 0，报告也不从检索/引用指标推断成功 | `benchmark/reports/second-batch-offline-20261101/summary.json` 与 `third-batch-offline-20261101/summary.json` 均为 `0/0, value=null` |
| 为什么历史版本查询 0/2 | 离线自然语言路由只走当前视图，不生成 `memory_view=history`；DecisionStore 本身支持历史/时点查询 | `decision-atlas`：`known_at_applied=false`、`current_version_correct=false`，实际拿到当前 C 而非 2025-02-20 的 B；`decision-zephyr`：`history_view_applied=false`、`historical_versions_present=false` |
| 指定时间失败是适配器、模型还是 Runtime 缺陷 | 当前证据指向**离线适配器限制**：不解析日期、不构造 `known_at`。Runtime/DecisionStore 的 `snapshot(..., known_at=...)` 与 history 视图可用，已由标签校验与既有测试覆盖；模型在线行为**未验证** | `benchmark/tasks/scoring.py:_known_at_applied` 在离线行返回 false；`validate_tasks` 用 `DecisionStore.snapshot` 成功校验截止日版本 |
| 既有真实模型记录能证明什么 | 能证明协议层修复有效：DeepSeek 第二次冒烟 17 次响应、0 协议错误、4/5 场景通过；不能证明语义任务成功、工具失败恢复或当前代码的在线表现 | 旧 `deepseek-smoke-*` 记录；旧 `cross_source` 失败后来确认是评测规则（工具次数判据）问题，已在评测侧修正，旧报告未改写 |
| 是否有独立可运行的 live ReAct 在线评测入口 | 之前没有 Benchmark 级入口；本轮新增 `benchmark/tasks/run_online.py`，复用正式 `ReActAgent.run_result`，不是简化 Agent | 默认只输出计划；需 `--online`、精确 origin、新输出目录 |

## 1. 修改文件与原因

本阶段只新增评测层文件，未改动产品运行时代码。

| 文件 | 原因 |
| --- | --- |
| `benchmark/tasks/public.json` | 16 道公开任务，覆盖 8 类任务；运行时可读 |
| `benchmark/tasks/labels.json` | 隐藏 Gold 标签：关键事实、可接受证据、时间/权威/冲突/弃答/恢复要求、人工复核规则 |
| `benchmark/tasks/scoring.py` | 题目校验、标签后置连接、确定性指标、人工评分门禁、聚合、失败假设诊断、运行配置与源码 hash |
| `benchmark/tasks/run_offline.py` | 复用 `benchmark.runner.run_backend` 的离线任务评测；无模型、无网络；运行时只传公开字段 |
| `benchmark/tasks/run_online.py` | 显式授权的在线 ReAct 评测入口；默认不建目录、不读凭据、不联网；禁用重定向；不持久化凭据与原始响应 |
| `benchmark/tasks/grade.py` | 对已有报告应用显式人工评分，不重跑任务；拒绝给 unsupported/runtime_error 行打成功分 |
| `benchmark/tasks/README.md` | 任务、标签、隔离、指标、命令与限制说明 |
| `tests/test_task_eval.py` | 18 项隔离/授权/评分测试 |
| `docs/THIRD_BATCH_EVALUATION_PREP.md` | 本报告 |

新增报告目录（均为新目录，未覆盖历史报告）：

- `benchmark/reports/third-batch-offline-20261101/`：原冻结 Benchmark 的 14 题离线回归。
- `benchmark/reports/third-batch-task-offline-20261101/`：16 题任务级离线评测。

## 2. 任务级评测与独立评分口径

题目集 `cma-task-eval-v1`，8 类各 2 题，共 16 题：

| 类别 | 任务 |
| --- | --- |
| 单来源事实 | `single-atlas`、`single-zephyr` |
| 跨来源调查 | `cross-atlas`、`cross-zephyr` |
| 多轮追踪 | `multi-atlas`、`multi-zephyr` |
| 决策形成与变化 | `decision-atlas`、`decision-zephyr` |
| 已审核决策 vs 普通讨论 | `authority-atlas`、`authority-zephyr` |
| 证据冲突 | `conflict-atlas`、`conflict-zephyr` |
| 证据不足时合理弃答 | `missing-atlas`、`missing-zephyr` |
| 工具失败恢复 | `recovery-atlas`、`recovery-zephyr` |

每题标签记录：用户问题、关键事实及标记组、可接受 Evidence ID/hash、必需或可接受来源范围、指定时点与版本/替代关系、权威层级预期、冲突双方、弃答与不确定性要求、注入失败、人工复核规则。

隔离边界：

- Runtime 只收到 `case_id/category/question/project_id/dataset_kind/corpus_snapshot_id/split`。
- `labels.json` 只在执行结束后由 `score_task` 连接；报告记录 `scoring_isolation=labels_joined_after_run_only`。
- `--task` 与 `--max-tasks` 只筛选公开任务，不把标签传给运行时。

指标分层：

- 协议合法率、模型调用成功率、工具调用成功率；
- Evidence 召回/覆盖率、引用有效率、引用可接受证据覆盖率、来源召回；
- 关键事实字面覆盖率（确定性标记，不等于语义正确）；
- 决策、时间、权威、冲突、弃答、工具恢复检查；
- 模型调用数、延迟、Token、成本状态。

`TaskSuccessRate` 与 `AppropriateAbstentionRate` **仅**来自人工显式评分。没有人工评分时值为 `null`。引用有效率为 1.0、Evidence 数量多、事实标记全覆盖都不构成任务成功。

## 3. 实际离线结果

### 3.1 测试

```text
python -m unittest discover -s tests
Ran 190 tests in 17.895s
OK
```

`tests/test_task_eval.py` 18 项全部通过。`pytest` 未安装，使用标准库 `unittest`。

### 3.2 原冻结 Benchmark 回归（14 题）

命令：`python -m benchmark.runner --output benchmark/reports/third-batch-offline-20261101 --split test`

| 指标 | workflow | react |
| --- | --- | --- |
| Hit@5 | 0.857 (12/14) | 0.786 (11/14) |
| Recall@5 | 0.714 | 0.607 |
| MRR | 0.800 | 0.655 |
| EvidenceCoverage | 0.714 | 0.714 |
| CitationValidityRate | 78/78 = 1.0 | 74/74 = 1.0 |
| TaskSuccessRate | null (0/0) | null (0/0) |
| 运行错误 | 2 | 0 |
| unsupported / ungraded | 5 / 9 | 5 / 9 |
| 模型调用 | 0 | 0 |

决策检查（两后端相同）：current_version 2/3；historical_versions 0/2；supersedes 1/1；pending_not_confirmed 1/1；world_unknown 3/3。

### 3.3 任务级离线评测（16 题 × 2 后端 = 32 行）

命令：`python -m benchmark.tasks.run_offline --output benchmark/reports/third-batch-task-offline-20261101`

- 任务集 SHA-256：`ec8cc585d3074ede09a6727ba40630776bb635be190e4ef89d97aab387a69341`
- 32 行中：`unsupported_offline` 14、`ungraded_offline_adapter` 16、`runtime_error` 2、`offline_fallback_not_real_model` 0、已人工评分 0。
- 总模型调用 0；总工具尝试 50。
- `TaskSuccessRate`、`AppropriateAbstentionRate`、`ProtocolValidityRate`：null（无模型调用 / 无人工评分）。

| 类别 | 工具成功率 | Evidence 召回 | 引用有效率 | 引用可接受覆盖 | 事实覆盖 | 来源召回 |
| --- | --- | --- | --- | --- | --- | --- |
| single_source | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| cross_source | 1.000 | 0.500 | 1.000 | 0.500 | 0.750 | 0.750 |
| multi_round | 1.000 | 0.500 | 1.000 | 0.500 | 0.750 | 0.667 |
| decision_history | 1.000 | 1.000 | 1.000 | 1.000 | 0.400 | 1.000 |
| reviewed_vs_discussion | 1.000 | 1.000 | 1.000 | 1.000 | 0.667 | 1.000 |
| conflict | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| insufficient | 1.000 | 1.000 | 1.000 | 1.000 | 0.667 | 1.000 |
| tool_failure | 0.000 (0/4) | 0.000 | 无引用 | 0.000 | 0.000 | 0.000 |
| **总体** | **0.880** | **0.750** | **1.000** | **0.750** | **0.625** | **0.808** |

操作型工具失败恢复率：**0/4 = 0.0**。这四行中两行是 workflow 运行时异常，两行是 react 重试同一失败工具、未切换来源。

诊断计数（同一假设可命中多行）：`offline_adapter_limitation` 16、`retrieval_recall_or_tool_selection_gap` 12、`cross_source_selection_gap` 10、`answer_omission_or_marker_strictness` 10、`candidate_evidence_not_covered_by_answer` 8、`history_view_not_requested` 4、`known_at_query_condition_not_applied` 4、`time_snapshot_inconsistent` 4、`tool_failure_recovery_gap` 4、`runtime_or_harness_error` 2。

## 4. 已知失败场景与原因分类

### 4.1 跨来源（cross_source）

- `cross-atlas`：两后端均只召回 1/2 可接受证据；观察到 chat/document/issue，但缺少 `required_sources` 中的 git_commit；事实覆盖 1/2。
- `cross-zephyr`：Evidence 召回 1/2、来源覆盖 2/2（git_commit + issue），事实覆盖 2/2。**事实标记可被非可接受证据满足**，进一步说明字面覆盖不能替代人工语义验证。
- 分类：离线适配器一次性路由到单一技能，缺少“沿引用换来源”的多步检索；不是引用有效性问题。真实模型是否补查提交，尚未验证。

### 4.2 指定时点与历史版本（known_at / historical_versions）

- `decision-atlas`：截至 2025-02-20 应回答 B，离线行拿到当前 C；`known_at_applied=false`、`snapshot_respects_cutoff=false`、事实覆盖 0/2。
- `decision-zephyr`：应展示 B→C 演变，`history_view_applied=false`、`historical_versions_present=false`、事实覆盖 2/3。
- 分类：离线适配器不解析时间条件、不请求 history 视图；DecisionStore 的快照/历史能力已实现。属于“查询条件构造 + 视图选择”缺口，不是存储缺陷。真实模型行为未验证。

### 4.3 工具失败恢复（tool_failure）

- workflow：注入 `TimeoutError` 后整个请求抛异常（`runtime_error=TimeoutError`），没有降级或换来源。
- react 离线：对同一失败技能重试 2 次，未切换来源；恢复率 0，披露覆盖 0.5。
- 分类：workflow 缺少工具失败包含（产品行为，不是评测器问题）；react 离线 fallback 缺少换技能策略。真实模型是否会换来源，未验证。

### 4.4 多轮追踪（multi_round）

- 两题均只召回 1–2/3 可接受证据，`multi-atlas` 缺 document 来源；离线只发起一次工具调用。
- 分类：离线适配器没有真正的多步追踪/预算内补查；“沿引用追查”未执行，不能据此评价模型规划能力。

### 4.5 权威、冲突与弃答

- `authority-*`：讨论与已审核决策均被观察到，`reviewed_vs_discussion_disclosed=true`；但事实覆盖 2/3，缺截止日/版本演变事实。
- `conflict-*`：react 行同时披露冲突双方并记录 conflict、终止原因 `conflict`；workflow 行虽披露双方，但 `termination_reason=not_reported_by_workflow`、未记录 conflict，因此确定性冲突信号为 false。
- `missing-*`：`missing-zephyr` 两行均披露“未正式批准、无确认版本”；`missing-atlas` 只披露 1/2 必需缺口，且未说明生产执行状态。离线终止原因不是 `insufficient`，不能据此判定弃答正确。
- 分类：部分为离线路由/组合限制，部分为评测器的确定性信号依赖终止原因；真实语义正确性待人工评分。

## 5. 真实模型在线评测命令

**本次未执行**。执行前需操作者授权，并确保语料仅为合成数据。

先查看计划（不联网、不建目录）：

```bash
python -m benchmark.tasks.run_online
```

小规模连通性验证（建议先跑 1 题）：

```bash
python -m benchmark.tasks.run_online \
  --online \
  --approved-origin https://api.siliconflow.cn \
  --output benchmark/reports/third-batch-online-smoke-20261101 \
  --task single-atlas
```

全量 16 题：

```bash
python -m benchmark.tasks.run_online \
  --online \
  --approved-origin https://api.siliconflow.cn \
  --output benchmark/reports/third-batch-online-20261101
```

要求：

- `--approved-origin` 必须与 `SILICONFLOW_BASE_URL` / `OPENAI_BASE_URL` 配置的 origin 完全一致（`scheme://host[:port]`，无路径、无凭据、无查询串）。
- 输出目录必须不存在；已存在时在发出任何请求前拒绝。
- 凭据只由 `ReActAgent` 内部读取，运行器不打印、不写入报告。
- 首个任务若为 `offline_fallback`，运行以 `not_executed_no_effective_credentials` 结束，不把它当真实模型结果。
- 在线报告保存 `config`：provider、origin、模型、运行时间、源码 SHA-256、Prompt hash、语料快照、题目集版本/hash、参数与预算；逐任务保存 Trace、调用的 Token、Evidence、终止原因、待评分项。
- 成本固定为 `unavailable_no_pricing`，不假设价格。

## 6. 任务量与调用上限

| 项目 | 默认值 | 说明 |
| --- | --- | --- |
| 任务数 | 16 | 8 类 × 2 |
| 每题最大模型轮次 | 5 | `--max-model-rounds` 可降低 |
| 每题最大工具调用 | 8 | `--max-tool-calls` 可降低 |
| 全量最大模型调用 | **80** | 16 × 5，硬上限 |
| 全量最大工具调用 | **128** | 16 × 8，硬上限 |
| 单题输出 Token 上限 | 2048 | 不可超过模型上下文预算 |
| 单题截止时间 | 120s | 超时以预算/超时终止记录 |

用 `--task <id>`（可重复）或 `--max-tasks N` 可把调用量等比例降低，例如 `--max-tasks 2` 最多 10 次模型调用。建议先 1 题验证协议与凭据，再全量。

## 7. 真实结果后的定向优化决策表

只有拿到真实模型报告并完成人工评分后才按此表行动；当前不据此改检索或 Runtime。

| 真实报告信号 | 优先假设 | 定向动作 | 暂不动作 |
| --- | --- | --- | --- |
| 工具成功率高、Evidence 召回高、事实覆盖/人工成功率低 | 答案遗漏或组合阶段丢失证据 | 检查 Trace 的 Observation→claim→answer，修 Prompt/组合逻辑 | 不改检索 |
| 工具成功率高、Evidence 召回低，且查询与问题同义 | 查询构造或工具选择不足 | 在真实 Trace 上定位查询改写/技能选择 | 不改存储与 Ledger |
| Evidence 召回高、引用有效率低 | 引用生成/回取问题 | 修引用约束与回取校验 | 不改召回 |
| `known_at_applied=false` 且无时间过滤调用 | 模型未使用时间能力 | 修工具 schema/Prompt 的时间参数暴露 | 不改 DecisionStore |
| `history_view_applied=false` 且问题要求演变 | 模型未请求历史视图 | 补 Prompt/工具说明并复测 | 不改数据模型 |
| 工具失败后无后续调用、恢复率 0 | 规划/降级策略不足 | 在 ReAct 规划层加“失败换来源”约束（需单独批准） | 不改失败注入 |
| 协议错误/非法 Action > 0 | 协议层问题 | 只修协议解析/校验 | 不调检索 |
| 冲突题只给单方或不记录冲突 | 冲突披露/终止原因问题 | 修冲突呈现与终止语义 | 不选边、不改标签 |
| 弃答题未说明缺口 | 弃答表达不足 | 修答案模板的不确定性披露 | 不降低弃答判据 |
| 延迟/调用数超预算 | 规划深度或预算配置 | 调整预算/规划步数并复测 | 不牺牲证据完整性 |
| 离线与在线同题差异大 | 离线适配器限制 | 以在线结果为准，离线只作回归 | 不用离线结果宣称模型能力 |

## 8. 边界声明

- 本阶段未自动运行真实模型，未读取/打印/修改 API Key。
- 未覆盖任何既有报告；新报告均写入新目录。
- 未硬编码测试答案；标签只用于执行后评分。
- 未新增 Agent 框架、MCP、Multi-Agent 或远程连接器。
- 未 Git commit/push。
- 所有离线行均标记 `offline_fallback` / `offline_no_model`，不得称为真实模型效果。
