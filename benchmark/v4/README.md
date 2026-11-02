# Phase 4 Benchmark V2（正式评测目录）

本目录是独立的 Phase 4 评测实现，与 `benchmark/tasks`（V1 冻结任务）、
`benchmark/v2`、`benchmark/v3` 的历史数据、报告和脚本分离。**不修改 V1 冻结任务、
Gold、Snapshot、Labels 或历史报告**，也不复用它们的分数。

默认执行模式是**离线、无模型、无网络**：不读取真实 `.env` 或 API Key，把模型配置
置空，并把 `urllib.request.urlopen` 替换为失败关闭。在线运行器只提供实现与受控测试，
默认不发起任何付费请求；本阶段未执行真实模型实验。

## 目录

| 路径 | 作用 |
|---|---|
| `build.py` | 确定性合成数据/任务/Gold 作者脚本 |
| `dataset/` | 冻结快照：`records.json`、`decisions.sql`、`tasks/public.json`、`tasks/gold.json`、`manifest.json` |
| `dataset.py` | 快照加载、哈希/数量/Gold 校验、SQLite 物化与克隆 |
| `runtime.py` | 在真实 ReAct / LangGraph Workflow 链路上执行公开任务 |
| `scoring.py` | 确定性自动评分、人工复核边界、聚合 |
| `indexing.py` | 为快照的每个项目构建并校验真实 FAISS 索引；失败即抛出 |
| `run_offline.py` | 离线正式评测入口 |
| `run_online.py` | 默认关闭、需显式确认的在线运行器（ReAct / Workflow） |
| `ablations.py` | Retrieval / Context 消融入口 |
| `acceptance.py` | 生成小规模真模型验收准备材料；不发起任何请求 |
| `acceptance/` | 冻结任务选择、Gold 复核清单、评分模板、终止/恢复流程 |
| `grade.py` | 合并具名人工评分，生成独立 graded 报告 |
| `reports/` | 运行输出（不提交，见 `.gitignore`） |

## 数据快照

当前冻结版本：

- `dataset_version = cma-v4-dataset-v1`
- `task_set_version = cma-v4-tasks-v1`
- `scoring_version = cma-v4-scoring-v1`
- `snapshot_id = cma-v4-658dd583ab18a47f`
- `task_set_sha256 = 8793116a0581fc71d5b4eb3d89677fe99ba36ee9f39c25dae00c24269c6644b1`
- 414 条原始观察、396 个 Evidence 身份、3 个合成项目、45 个任务
- 9 个任务类别 × 5；难度 easy 5 / medium 25 / hard 15

数据完全合成，覆盖 Chat/IM、Issue、Git Commit、文档、日志、会议记录、代码与
Decision Memory。每类记录都包含项目身份、人员/角色、事件时间、采集/入库时间、
来源 URI 和 locator；文档包含 v1/v2 版本，决策包含 proposal、review、正式确认、
版本历史与 known_at；部分记录带有 deleted / orphaned 生命周期和冲突/缺失证据。

`manifest.json` 冻结四个数据文件的字节数与 SHA-256、记录数、Evidence 身份数、
快照 ID、任务集哈希和标注状态。`dataset.load()` 在每次评测前重新校验；任何文件被
修改都会拒绝加载。Gold 是作者阶段的合成标注，
`annotation_status = synthetic_authored_pending_independent_human_review`，不冒充
已完成独立人工审核。

### 时间语义

每条 Evidence 区分：

- `event_time`：事件实际发生时间
- `ingested_at`：被系统采集/入库的时间
- 决策的 `proposed_at`：提案时间
- 决策的 `known_at` / `confirmed_at`：正式确认时间与可知时点

**历史文档文本不是正式评审结论。** `decision_history` 任务的 Gold 明确区分截止时点
的已确认版本与尚未确认的提案，禁止把后者说成当前状态。

### 任务与 Gold

任务类别：`single_source`、`cross_source`、`multi_step`、`decision_history`、
`conflict`、`insufficient`、`budget`、`tool_failure`、`multi_turn`。

公开任务只包含 `task_id/category/project_id/dataset_kind/split/difficulty/question/
task_set_version`（多轮任务另含 `turns`）。运行时不接收 Gold。隐藏标签独立存放在
`tasks/gold.json`，包含：

- `expected_facts`：结构化事实（日期、版本、人员、Issue/日志 ID、组件、决策状态），
  支持 `values` 或 `marker_groups`，每个事实显式声明 `required: true/false`；可选事实
  只在实际出现时计入 OptionalFactAccuracy，缺失不判错
- `forbidden_inferences`：禁止推断的表述组；命中即记为 `forbidden_inference`，用于
  检查把提案说成已确认、把单方文档说成裁决、把预算中断说成完整结论等错误
- `required_evidence_groups`：必要证据组，组内 `any_of` 可接受替代证据；conflict 任务
  把执行日志作为冲突值的等价替代来源
- `acceptable_alternative_evidence`：从多来源证据组自动导出的等价替代清单，供人工
  复核替代是否真的等价；替代引用同样校验存在性、`content_hash` 与 current 状态
- `expected_decision_state`：`known_at`、当前版本、允许版本、禁止说成已确认的版本/提案
- `expected_conflicts`：冲突字段与是否必须同时披露双方
- `expected_abstention`：是否应拒答/声明不足，及必须披露的内容
- `expected_limitations`：必须说明的限制
- `scoring_rubric`、`human_review_rules`、`difficulty`、`task_set_version`
- `injected_failure`（工具失败注入配置）与 `budget_override`（预算限制配置）

多轮任务有显式 `turns` 和每轮 `evaluation_target`；独立单轮问题不会被拆成多轮。
工具失败通过固定 `{"skill": ..., "error": ...}` 注入，可复现；预算任务使用
`Budget` 的显式上限。

## 自动评分与人工边界

`scoring.py` 输出逐任务指标：

| 指标 | 说明 |
|---|---|
| EvidenceRecall@K | 前 10 个实际交付 Evidence 对必要证据组的覆盖；组内替代证据可命中 |
| EvidenceRecall_all_delivered | 所有交付 Evidence 的覆盖 |
| CitationValidity | 引用是否存在、`content_hash` 是否匹配、是否在当前 Ledger、Claim 原文是否出现在被引内容中 |
| FactAccuracy | 必选结构化 Gold 事实命中率，不做纯关键词匹配 |
| OptionalFactAccuracy | 实际出现的可选事实命中率；缺失的可选事实不参与分母 |
| ForbiddenInferenceRate | 命中 `forbidden_inferences` 禁止表述组的运行比例 |
| AppropriateAbstentionRate | 确定性拒答信号，不是语义评分 |
| ConflictDetectionRate | 冲突字段双方是否都被披露 |
| DecisionStateAccuracy | known_at 与当前确认版本是否正确，是否误报提案为已确认 |
| ToolFailureRecoveryRate | 注入失败后是否仍完成/明确披露 |
| False refusal / unsupported answer | 不应拒答却拒答、应拒答却给出无依据答案的信号 |

引用完整性与“引用是否真的支持 Claim”是两件事：前者可自动检查，后者只进入人工
复核，不由脚本推断。每个聚合结果同时给出 `metric_coverage`（measured / total），
避免把只覆盖部分任务的指标误读为全体指标；例如离线基线中大量运行没有引用，
CitationValidity 只在存在引用的运行上测量。

**TaskSuccessRate 在独立人工评分完成前始终为 `null`**。自动评分只生成
`pending_grading.json` 与模板；`grade.py` 只接受具名 reviewer 对每个
`(backend, task_id)` 的显式布尔判断，并且写入独立 `graded-*` 目录，绝不回写原始报告。
自动指标不会被当成任务成功率。

## 离线运行

```powershell
# 全量 45 任务 × 2 backends
python -m benchmark.v4.run_offline --output benchmark/v4/reports/offline-<新目录>

# 子集
python -m benchmark.v4.run_offline --category conflict --backend react --limit 5 `
  --output benchmark/v4/reports/offline-conflict-<新目录>

# 只打印计划，不写目录
python -m benchmark.v4.run_offline --plan-only
```

`--retrieval-mode dense|hybrid` 会在运行任何任务前，为快照的全部项目构建并校验真实
FAISS 索引（`indexing.py`）。索引构建或校验失败、任一检索事件缺少 `index_version`、
或实际回退为 BM25 时，正式运行会中止并删除刚创建的报告目录；不会把 fallback 记为
dense/hybrid 成功。结果行的 `raw.retrieval` 保留每个任务的 `requested_modes`、
`effective_modes`、`index_versions`、`source_revisions` 与 `fallback_reasons`。

离线 ReAct 使用 `offline_fallback`（关键词路由，无模型决策循环），Claim Ledger 和
引用校验仍然运行；Workflow 使用固定 LangGraph 管线，无 Claim Ledger、无预算协议、
无 known_at 过滤。因此离线结果只能作为**同一快照、同一任务、无模型条件下的架构
基线**，不是真实模型能力分数，也不是 ReAct 与 Workflow 的公平语义对比。两者共享：
快照、任务、项目 scope、Evidence DB、Gold、scorer、检索模式、无 provider 凭据。

每次运行输出：`summary.json`、`results.jsonl`、`config.json`、
`failure_cases.json`、`pending_grading.json`、`report.md`。配置记录
dataset/task/scoring 版本、快照与文件哈希、源码哈希、Git HEAD/worktree 状态、
Agent/Workflow/Retrieval/Context 版本、模型配置（离线为 `null`）、超时、预算和
已知能力差异。已存在的输出目录一律拒绝覆盖，避免覆盖历史报告。

## 在线运行器（默认关闭）

```powershell
# 默认只输出计划，不读凭据、不发请求
python -m benchmark.v4.run_online --plan-only

# 只有在单独授权后才可显式开启；本阶段未执行
python -m benchmark.v4.run_online --backend react `
  --online --i-understand-this-makes-paid-model-calls `
  --provider <provider> --model <model> --base-url https://<exact-origin> `
  --approved-origin https://<exact-origin> `
  --max-tasks 5 --max-model-calls 20 --max-budget-tokens 20000 `
  --output benchmark/v4/reports/online-<新目录>
```

`--backend` 支持 `react`（默认）与 `workflow`。Workflow 复用生产
`CoordinatorAgent` / LangGraph 固定管线，仅在线时启用 LLM 路由；它没有原生预算协议，
因此由外部 `ModelHttpGuard` 对每个请求计数并在 `max_model_calls` /
`max_budget_tokens` 处硬停止。两个 backend 使用同一快照、任务集、项目 scope、
检索模式、scorer、provider/model 和共享上限；Workflow 在 Claim Ledger、known_at、
多轮调查和预算协议上的固有能力差异会在计划、配置和结果中显式披露，**不得把架构
差异解释为 ReAct 推理机制的因果提升**。

控制项：默认 dry-run；必须显式确认；必须给出 provider/model/base URL 和精确 HTTPS
origin；独立 `run_id`；任务子集、最大任务数、最大模型调用数、最大预算 token、
单次输出上限、最大模型轮次、最大工具调用、超时、有限重试；402（余额不足）立即停止，
429（限流）/503（服务不可用）有限重试并分类记录；结果逐任务保留，支持 `--resume`
续跑未完成任务；报告只写环境变量名和脱敏后的 origin，不写 key、完整敏感 prompt。
离线路径不会发起任何网络请求。

## 消融

```powershell
python -m benchmark.v4.ablations --output benchmark/v4/reports/ablations-<新目录>
python -m benchmark.v4.ablations --arm bm25_phase3 --arm bm25_phase2_simulated `
  --output benchmark/v4/reports/ablations-subset-<新目录>
```

臂：`bm25_phase3`、`dense_phase3`、`hybrid_phase3`、`bm25_phase2_simulated`、
`hybrid_phase2_simulated`。所有臂共享快照、任务、Gold、scorer 与项目 scope，仅改变
Retrieval / Context 配置；允许只跑部分臂，报告中标记 `partial`，不补齐、不推算。

- Dense/Hybrid 臂会先真实构建并校验 FAISS 索引；索引准备失败则整个臂中止。运行中
  任一检索事件回退为 BM25 时，该臂标记 `executed_with_bm25_fallback_not_<mode>` 并
  不冒充 dense/hybrid 结果；只有 requested 与 effective 一致且存在 `index_version`
  时才标记 `executed_requested_configuration`。
- `phase2_simulated` 用当前 ledger 重建 Phase 2 请求形状，是**显式仿真**，不是历史
  Phase 2 报告复跑。
- Phase 1 相关性策略、阈值、现有检索算法和 Phase 3 Context 策略均未为追求分数而修改。

## 小规模真模型验收准备

`benchmark/v4/acceptance/` 由 `acceptance.py` 从冻结快照生成，**不读取 Key、不发起
请求、不产生费用**。该目录固定 10 个任务，覆盖单源、跨源、多步调查、历史决策、
冲突、证据不足、多轮追问、两种工具失败和显式预算限制，并给出：

- 任务 ID、类别、难度、轮数、选择理由与注入配置；
- 逐任务 Gold 人工复核清单（必要证据组、替代证据、结构化事实、禁止推断、决策/
  冲突/拒答/限制）；
- ReAct / Workflow 共享控制项、per-backend 调用上限、token 与成本估算方法；
- `(task_id, backend)` 人工评分模板；
- 结果汇总模板与 402/429/503、索引失败、预算耗尽的终止/恢复流程。

```powershell
python -m benchmark.v4.acceptance            # 生成到 benchmark/v4/acceptance
python -m benchmark.v4.acceptance --plan-only
```

本轮只准备材料，未执行真模型调用。`TaskSuccessRate` 只有在 20 个
`(task_id, backend)` 都有具名 reviewer 的显式判断后才可计算。

## 人工评分

```powershell
# 导出待评分模板（不写文件）
python -m benchmark.v4.grade --report benchmark/v4/reports/<run> --template

# 合并具名评分，生成独立 graded 目录
python -m benchmark.v4.grade --report benchmark/v4/reports/<run> --grades grades.json
```

只有所有已执行 run 都有显式布尔 `task_success` 时，`TaskSuccessRate` 才会变为数值；
否则保持 `null` 并给出 `pending_human_review`。缺失的语义支持、冲突解释和决策解释
不会被自动补全。

## 已知限制

- 数据与 Gold 为合成作者产物，尚未完成独立人工审核与互评者一致性测量。
- 离线运行没有模型推理，指标反映检索/上下文/链路管线，不是语义能力。
- ReAct 与 Workflow 的离线差异包含架构固有能力差异，不是受控模型对比。
- Dense/Hybrid 正式运行必须成功构建并校验索引；准备失败或运行中回退即中止，不以
  BM25 结果代替。
- Workflow 在线运行没有原生 Claim Ledger、known_at、多轮调查或预算协议；能力差异
  已披露，结果只作端到端架构对比。
- `TaskSuccessRate`、语义支持、人工冲突/决策解释在人工复核前不可报告。
- provider token、实际成本和真实模型延迟只能在获得授权的在线实验中测量；离线
  相关字段为 `null` 或估算值。
- 本阶段未调用任何真实 LLM，未读取 API Key；验收实验仅准备、未执行。

## 测试

```powershell
python -m unittest tests.test_phase4_benchmark -v
python -m unittest discover -s tests
```
